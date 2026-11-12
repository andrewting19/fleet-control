#!/usr/bin/env python3
"""Fleet Control command line.

  python3 fleet.py serve            run the server (http://localhost:10400)
  python3 fleet.py serve --demo     run it with made-up data, no setup needed
  python3 fleet.py check            run every collector once and report what works
  python3 fleet.py export-demo DIR write a static demo site (made-up data) to DIR
  python3 fleet.py projects         list working folders by token use, with the project each maps to
  python3 fleet.py service install  start at login (launchd on macOS, systemd --user on Linux)
  python3 fleet.py service remove
"""
import argparse
import os
import shutil
import subprocess
import tempfile
import sys
import time

if sys.version_info < (3, 11):
    sys.exit("Fleet Control needs Python 3.11 or newer on the machine that runs the server (probes need only 3.6).")

from fleetctl import accounts, config, hosts, sources, usage
from fleetctl.app import App
from fleetctl.server import serve
from fleetctl.util import log

LABEL = "dev.fleet-control"


def cmd_serve(args):
    cfg = config.load(args.config)
    if args.port:
        cfg["port"] = args.port
    app = App(cfg, demo=args.demo)
    app.start()
    log("Fleet Control%s on http://%s:%d" % (" (demo)" if args.demo else "", "localhost" if cfg["bind"] in ("127.0.0.1", "0.0.0.0") else cfg["bind"], cfg["port"]))
    serve(app)


def cmd_export_demo(args):
    """A static copy of the page with made-up data, for GitHub Pages or any file host.
    The page reads state.json and moves its times to the current time."""
    cfg = config.load(args.config)
    with tempfile.TemporaryDirectory() as tmp:
        cfg["data_dir"] = tmp
        app = App(cfg, demo=True)
        from fleetctl import demo
        demo.fill(app)
        app.build()
        snap = app.snapshot
    ui = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
    os.makedirs(args.dir, exist_ok=True)
    shutil.copytree(ui, args.dir, dirs_exist_ok=True)
    with open(os.path.join(args.dir, "state.json"), "wb") as f:
        f.write(snap)
    print("wrote %s (open index.html through a web server, for example: python3 -m http.server -d %s)" % (args.dir, args.dir))


def cmd_check(args):
    """One pass of every collector, with a plain report. Exit code 1 if something failed."""
    cfg = config.load(args.config)
    print("config:", cfg["path"] or "none (defaults: this machine, Codex account)")
    app = App(cfg)
    bad = 0

    def line(ok, what, detail):
        nonlocal bad
        bad += not ok
        print("  %s %-28s %s" % ("ok  " if ok else "FAIL", what, detail))

    print("machines:")
    for h in cfg["hosts"]:
        if not (h.get("local") or h.get("ssh")):
            line(True, h["name"], "status from Tailscale only")
            continue
        t = time.time()
        hosts.probe(app.store, h)
        d = app.store.hosts.get(h["name"]) or {}
        if d.get("ok"):
            line(True, h["name"], "%s, %d cores, %d disks (%.1f s)" % (d.get("os"), d.get("cores"), len(d.get("disks", [])), time.time() - t))
        else:
            line(False, h["name"], d.get("error", "no data"))
        if h.get("scan"):
            usage.scan(app.store, h)
            sc = app.store.scans.get(h["name"])
            err = app.store.errors.get("scan:" + h["name"])
            line(not err, "  agent sessions", err["msg"] if err else "%d sessions with token use in the last 8 days" % len(sc.get("sessions", [])))
    print("accounts:")
    for name, src in app.sources:
        try:
            res = src.collect(True)
            accts = [accounts.normalize(a, name) for a in res.get("accounts", [])]
            line(True, name, "%d accounts" % len(accts))
            for a in accts:
                q = "no quota data" if a["weekly_used"] is None else "%.0f%% of week used" % a["weekly_used"]
                print("         %-10s %-32s %-10s %s%s" % (a["provider"], a["label"][:32], a["plan"] or "", q, "" if a["status"] == "ok" else ", " + a["status"]))
        except Exception as e:
            line(False, name, str(e)[:200])
    print("titles:")
    for conf, fn in app.titles_src:
        try:
            line(True, conf["type"], "%d named sessions" % len(fn(conf)))
        except Exception as e:
            line(False, conf["type"], str(e)[:200])
    if cfg["tailscale"]["enabled"]:
        app.tailscale()
        err = app.store.errors.get("tailscale")
        line(not err, "tailscale", err["msg"] if err else "%d peers" % len(app.store.tailscale))
    print("\n%s" % ("All collectors work." if not bad else "%d collector(s) failed. Fix them in config.toml, then run check again." % bad))
    sys.exit(1 if bad else 0)


def cmd_projects(args):
    """Help to write [[projects]] rules: the busiest working folders of the last 7 days and how they map now."""
    cfg = config.load(args.config)
    app = App(cfg)
    for h in cfg["hosts"]:
        if h.get("scan"):
            usage.scan(app.store, h)
    now, by = time.time(), {}
    for host, sc in app.store.scans.items():
        for s in sc.get("sessions", []):
            c = sum(app.models.cost(m, v) for hs, mv in s["bk"].items() if now - int(hs) < 7 * 86400 for m, v in mv.items())
            k = (host, s.get("cwd") or "")
            by[k] = by.get(k, 0) + c
    rows = sorted(by.items(), key=lambda x: -x[1])[: args.top]
    print("%-9s %-12s %-24s %s" % ("$ weight", "host", "project / sub", "working folder"))
    for (host, cwd), c in rows:
        p, sub = app.projects(cwd)
        print("%9.2f %-12s %-24s %s" % (c, host[:12], (p + (" / " + sub if sub else ""))[:24], cwd))


def cmd_service(args):
    here = os.path.dirname(os.path.abspath(__file__))
    py, script = sys.executable, os.path.join(here, "fleet.py")
    extra = ["--config", os.path.abspath(args.config)] if args.config else []
    if sys.platform == "darwin":
        path = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)
        logf = os.path.expanduser("~/Library/Logs/fleet-control.log")
        uid = os.getuid()
        if args.action == "remove":
            subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (uid, LABEL)])
            os.path.exists(path) and os.unlink(path)
            return print("removed", path)
        argv = "".join("<string>%s</string>" % a for a in [py, script, "serve"] + extra)
        with open(path, "w") as f:
            f.write("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key><array>%s</array>
  <key>WorkingDirectory</key><string>%s</string>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>%s</string><key>StandardErrorPath</key><string>%s</string>
</dict></plist>
""" % (LABEL, argv, here, logf, logf))
        subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (uid, LABEL)], capture_output=True)
        for i in range(10):  # bootout finishes in the background; bootstrap fails until it is done
            if subprocess.run(["launchctl", "bootstrap", "gui/%d" % uid, path], capture_output=True).returncode == 0:
                break
            time.sleep(1)
        else:
            sys.exit("launchctl bootstrap failed; try again in a few seconds")
        print("installed %s\nlog: %s\nrestart: launchctl kickstart -k gui/%d/%s" % (path, logf, uid, LABEL))
    else:
        path = os.path.expanduser("~/.config/systemd/user/fleet-control.service")
        if args.action == "remove":
            subprocess.run(["systemctl", "--user", "disable", "--now", "fleet-control"])
            os.path.exists(path) and os.unlink(path)
            return print("removed", path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("[Unit]\nDescription=Fleet Control\nAfter=network-online.target\n\n[Service]\nExecStart=%s\nWorkingDirectory=%s\n"
                    "Restart=always\n\n[Install]\nWantedBy=default.target\n" % (" ".join([py, script, "serve"] + extra), here))
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "fleet-control"], check=True)
        print("installed %s\nlog: journalctl --user -u fleet-control -f\n"
              "To keep it running after logout: loginctl enable-linger $USER" % path)


def main():
    p = argparse.ArgumentParser(description="Fleet Control: machines, agent accounts and token use on one page.")
    p.add_argument("--config", help="path to config.toml (default: ./config.toml or $FLEET_CONFIG)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the server")
    s.add_argument("--demo", action="store_true", help="made-up data, no setup needed")
    s.add_argument("--port", type=int)
    sub.add_parser("check", help="run every collector once and report what works")
    s = sub.add_parser("export-demo", help="write a static demo site with made-up data")
    s.add_argument("dir")
    s = sub.add_parser("projects", help="list working folders by token use and their project")
    s.add_argument("--top", type=int, default=40)
    s = sub.add_parser("service", help="start at login")
    s.add_argument("action", choices=["install", "remove"])
    args = p.parse_args()
    try:
        {"serve": cmd_serve, "check": cmd_check, "export-demo": cmd_export_demo, "projects": cmd_projects, "service": cmd_service}[args.cmd](args)
    except config.ConfigError as e:
        sys.exit("config error: %s" % e)


if __name__ == "__main__":
    main()
