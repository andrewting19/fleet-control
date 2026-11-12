"""Wire the collectors to a schedule and build the page state (GET /api/state) from their results."""
import concurrent.futures as cf
import json
import os
import shutil
import threading
import time
import traceback

from . import accounts, hosts, sources, usage
from .models import Models
from .projects import Projects
from .store import Store
from .util import log, run


class App:
    def __init__(self, cfg, demo=False):
        self.cfg = cfg
        self.demo = demo
        self.store = Store(os.path.join(cfg["data_dir"], "demo") if demo else cfg["data_dir"])
        self.models = Models(cfg["providers"], cfg["prices"])
        self.projects = Projects(cfg["projects"])
        self.sources = [] if demo else sources.account_sources(cfg, self.store)
        self.titles_src = [] if demo else sources.title_sources(cfg)
        self.snapshot = b"{}"
        self._titles = ({}, 0)
        self._last_refresh = 0

    # ------------------------------------------------------------ collectors
    def probe_all(self):
        hs = [h for h in self.cfg["hosts"] if h.get("local") or h.get("ssh")]
        if hs:
            with cf.ThreadPoolExecutor(len(hs)) as ex:
                list(ex.map(lambda h: hosts.probe(self.store, h), hs))
        self.build()

    def scan_all(self):
        hs = [h for h in self.cfg["hosts"] if h.get("scan")]
        if hs:
            with cf.ThreadPoolExecutor(len(hs)) as ex:
                list(ex.map(lambda h: usage.scan(self.store, h), hs))
        self.store.updated["scan"] = time.time()
        self.build()

    def accounts(self, force=False):
        now = time.time()
        refresh = force or now - self._last_refresh >= self.cfg["intervals"]["accounts_refresh"]
        if refresh:
            self._last_refresh = now
        accounts.collect(self.store, self.sources, refresh)
        self.build()

    def tailscale(self):
        ts = self.cfg["tailscale"]
        cli = os.path.expanduser(ts.get("cli") or "") or shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
        try:
            d = json.loads(run([cli, "status", "--json"], 20))
        except Exception as e:
            self.store.set_error("tailscale", e)
            return
        peers = {}
        for p in list((d.get("Peer") or {}).values()) + [d.get("Self") or {}]:
            info = {"online": bool(p.get("Online")) or p is d.get("Self"), "last_seen": p.get("LastSeen"), "os": p.get("OS")}
            peers[(p.get("HostName") or "").lower()] = info
            peers[(p.get("DNSName") or "").split(".")[0].lower()] = info
        with self.store.lock:
            self.store.tailscale = peers
        self.store.set_error("tailscale", None)

    def titles(self):
        cached, at = self._titles
        if time.time() - at < 120:
            return cached
        out = {}
        for conf, fn in self.titles_src:
            try:
                out.update(fn(conf))
            except Exception as e:
                self.store.set_error("titles:" + conf["type"], e)
        self._titles = (out, time.time())
        return out

    # ------------------------------------------------------------ state
    def build(self):
        now = time.time()
        st = self.store
        with st.lock:
            try:
                hv = hosts.view(self.cfg, st, self.projects, now)
                av = accounts.view(st, now)
                uv = usage.view(st, self.models, self.projects, self.titles(), now)
                for h in hv:
                    h["active_sessions"] = uv["active_sessions"].get(h["name"])
                found = []

                def add(level, area, text, who=None, fix=None):
                    found.append({"level": level, "area": area, "text": text, "who": who, "fix": fix})

                hosts.issues(hv, add)
                accounts.issues(av, self.cfg["providers"], st.source_issues, now, add)
                for k, e in st.errors.items():
                    if now - e["at"] < 1800 and not k.startswith("probe:"):
                        add("warn", "collector", "%s failed: %s" % (k, e["msg"][:120]))
                found.sort(key=lambda x: {"crit": 0, "warn": 1, "info": 2}[x["level"]])
                level = next((lv for lv in ("crit", "warn") if any(i["level"] == lv for i in found)), "ok")
                used = {a["provider"] for a in av} | {p for s in uv["series"].values() for p in s}
                prov = {k: {f: v[f] for f in ("name", "color", "icon")} for k, v in self.cfg["providers"].items() if k in used}
                snap = {"now": now, "level": level, "issues": found, "hosts": hv, "accounts": av,
                        "pools": accounts.pools(av, {k: self.cfg["providers"][k] for k in prov}, now), "usage": uv,
                        "meta": {"title": self.cfg["title"], "providers": prov, "project_colors": self.cfg["ui"]["project_colors"],
                                 "demo": self.demo},
                        "updated": st.updated, "errors": st.errors, "started": st.started}
                self.snapshot = json.dumps(snap, default=list).encode()
            except Exception:
                log("build failed", traceback.format_exc())

    # ------------------------------------------------------------ schedule
    def every(self, sec, fn, name=None):
        def loop():
            while True:
                t = time.time()
                try:
                    fn()
                except Exception:
                    log(fn.__name__, "failed", traceback.format_exc())
                time.sleep(max(1, sec - (time.time() - t)))
        threading.Thread(target=loop, daemon=True, name=name or fn.__name__).start()

    def start(self):
        iv = self.cfg["intervals"]
        if self.demo:
            from . import demo
            demo.fill(self)
            self.every(iv["probe"], lambda: (demo.tick(self), self.build()))
            return
        if self.cfg["tailscale"]["enabled"]:
            self.every(iv["tailscale"], self.tailscale)
        # One loop per machine and job: a slow or dead machine never delays the others.
        for h in self.cfg["hosts"]:
            if h.get("local") or h.get("ssh"):
                self.every(iv["probe"], lambda h=h: hosts.probe(self.store, h), "probe:" + h["name"])
            if h.get("scan"):
                self.every(iv["scan"], lambda h=h: usage.scan(self.store, h), "scan:" + h["name"])
        self.every(iv["accounts"], self.accounts)
        # The page data is rebuilt on its own timer, so it stays current while collectors wait on busy machines.
        self.every(5, self.build)

    def refresh(self):
        if not self.demo:
            threading.Thread(target=lambda: (self.accounts(force=True), self.scan_all()), daemon=True).start()
