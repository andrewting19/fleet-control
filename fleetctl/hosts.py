"""Machines: run the probe on each host and turn the raw numbers into a status with reasons."""
import os
import time

from .util import gb, run_probe


def probe(store, host):
    t = time.time()
    try:
        d = run_probe(host, "host_probe.py", 30)
    except Exception as e:
        with store.lock:
            prev = store.hosts.get(host["name"]) or {}
            prev.update({"error": str(e)[:300], "fail_at": t})
            if host.get("local") and prev.get("load"):
                # The probe can fail on an overloaded machine. The load average costs nothing to read, so keep it current.
                prev["load"] = list(os.getloadavg())
            store.hosts[host["name"]] = prev
        store.set_error("probe:" + host["name"], e)
        return
    d["at"] = t
    d["ok"] = True
    with store.lock:
        prev = store.hosts.get(host["name"]) or {}
        d["swap_rate"] = swap_rate(prev, d, t)
        page = d.get("page_size") or 4096
        d["swap_in_mbps"] = round(d["swap_rate"].get("in", 0) * page / 1048576, 2)
        d["swap_out_mbps"] = round(d["swap_rate"].get("out", 0) * page / 1048576, 2)
        store.hosts[host["name"]] = d
        hist = store.load_hist.setdefault(host["name"], [])
        hist.append([int(t), round(d["load"][0], 2), round(d.get("cpu_util") or 0, 1),
                     round(100 * (1 - d["mem_avail"] / max(1, d["mem_total"])), 1), d["swap_in_mbps"], (d.get("psi") or {}).get("some")])
        del hist[:-720]
    store.set_error("probe:" + host["name"], None)


def swap_rate(prev, cur, t):
    """Pages per second read from and written to swap since the last probe. Falls back to the probe's own 1 s sample."""
    pc, pt, c = prev.get("swap_counters"), prev.get("at"), cur.get("swap_counters")
    if pc and c and pt and 0 < t - pt < 300 and c.get("in", 0) >= pc.get("in", 0):
        return {k: max(0, (c.get(k, 0) - pc.get(k, 0)) / (t - pt)) for k in ("in", "out")}
    return cur.get("swap_rate_1s") or {}


def view(cfg, store, project_of, now):
    th = cfg["thresholds"]
    out = []
    for h in cfg["hosts"]:
        d = store.hosts.get(h["name"]) or {}
        ts = store.tailscale.get((h.get("tailscale") or h["name"]).lower())
        v = {"name": h["name"], "label": h.get("label") or h["name"], "critical": h.get("critical", True),
             "hidden": h.get("hidden", False), "tailscale": ts}
        if not (h.get("local") or h.get("ssh")):
            v["status"] = "online" if ts and ts.get("online") else "offline"
            out.append(v)
            continue
        if not d.get("load"):
            v.update(status="down", error=d.get("error") or "no data yet")
            out.append(v)
            continue
        reachable = now - d.get("at", 0) < 180
        cores = d.get("cores") or 1
        hist = store.load_hist.get(h["name"], [])
        cpu_s = [x[2] for x in hist[-3:]] or [d.get("cpu_util") or 0]
        v.update({"os": h.get("os") if h.get("os") not in (None, "posix") else (d.get("os") or "").lower().replace("darwin", "macos"),
                  "hostname": d.get("hostname"), "cores": cores, "load": d["load"], "load_ratio": round(d["load"][0] / cores, 2),
                  "cpu": round(sum(cpu_s) / len(cpu_s), 1), "iowait": d.get("iowait"),
                  "mem_total": d.get("mem_total"), "mem_avail": d.get("mem_avail"),
                  "mem_pct": round(100 * (1 - d.get("mem_avail", 0) / max(1, d.get("mem_total", 1))), 1),
                  "disks": [dict(x, pct=round(100 * x["used"] / max(1, x["total"]), 1)) for x in d.get("disks", [])],
                  "gpus": d.get("gpus", []), "uptime": d.get("uptime"), "at": d.get("at"), "age": int(now - d.get("at", now)),
                  "reachable": reachable, "error": None if reachable else d.get("error"), "hist": hist[-180:]})
        v["procs"], v["projects"] = _procs(d, project_of)
        v["jobs"] = d.get("jobs") or []
        _swap(v, d, h, hist, project_of)
        v["status"], v["reasons"] = _status(v, h, th)
        hog = next((e for e in v["projects"] if e["project"] != "system"), None) or (v["projects"][0] if v["projects"] else None)
        v["hog"] = hog and {"project": hog["project"], "cores": round(hog["cpu"] / 100, 1),
                            "sub": max(hog["subs"], key=hog["subs"].get) if hog["subs"] else ""}
        out.append(v)
    return out


def _procs(d, project_of):
    projs, procs = {}, []
    for p in d.get("procs", []):
        proj, sub = project_of(p.get("cwd"), p.get("cmd"))
        e = projs.setdefault(proj, {"project": proj, "cpu": 0.0, "rss": 0, "n": 0, "subs": {}})
        e["cpu"] += p["cpu"]
        e["rss"] += p.get("rss", 0)
        e["n"] += 1
        if sub:
            e["subs"][sub] = e["subs"].get(sub, 0) + p["cpu"]
        procs.append({"pid": p["pid"], "cpu": p["cpu"], "rss": p.get("rss", 0), "comm": p["comm"], "cmd": p["cmd"][:200],
                      "cwd": p.get("cwd", ""), "user": p.get("user", ""), "age": p.get("age"), "project": proj, "sub": sub})
    return sorted(procs, key=lambda p: -p["cpu"])[:25], sorted(projs.values(), key=lambda e: -e["cpu"])


def thrash_level(os_name, free_pct, swap_in_mbps, psi=None, mem_level=None):
    """Thrashing means the machine keeps moving pages between RAM and disk, so work stalls.
    A full swap file alone is not thrashing. Returns "ok", "warn" or "crit"."""
    if os_name == "windows":
        # The probe sends min(hard-fault bytes, reads on the page-file drive). That still can include mapped-file
        # reads on the same drive, so count it only when free memory is low too.
        if free_pct <= 5 and swap_in_mbps >= 20:
            return "crit"
        if free_pct <= 10 and swap_in_mbps >= 5:
            return "warn"
        return "ok"
    if psi and psi.get("some") is not None:
        # Linux PSI: the share of time that tasks stall on memory.
        some, full = psi["some"], psi.get("full") or 0
        if full >= 10 or some >= 40:
            return "crit"
        if some >= 20 or (some >= 10 and swap_in_mbps >= 0.4):
            return "warn"
        return "ok"
    # macOS has no PSI: use the swap-in rate and the kernel's free-memory level.
    if swap_in_mbps >= 10:
        return "crit"
    if swap_in_mbps >= 2 or (mem_level is not None and mem_level <= 10 and swap_in_mbps >= 0.4):
        return "warn"
    return "ok"


def _swap(v, d, h, hist, project_of):
    rin = [x[4] for x in hist[-3:] if x[4] is not None] or [d.get("swap_in_mbps", 0)]
    avg_in = sum(rin) / len(rin)
    psi = d.get("psi") or {}
    free_pct = 100 - v["mem_pct"]
    lvl = thrash_level(v["os"], free_pct, avg_in, psi, d.get("mem_free_level"))
    why = []
    if lvl != "ok":
        why.append("reads %.1f MB/s back from swap" % avg_in)
        if psi.get("some") is not None:
            why.append("tasks wait for memory %.0f%% of the time" % psi["some"])
    tot, used = d.get("swap_total") or 0, d.get("swap_used") or 0
    top = []
    for p in d.get("mem_top") or []:
        proj, sub = project_of(p.get("cwd"), p.get("cmd"))
        top.append(dict(p, project=proj, sub=sub))
    v.update({"swap_total": tot, "swap_used": used, "swap_pct": round(100 * used / tot, 1) if tot else None,
              "swap_in_mbps": round(avg_in, 2), "psi": psi, "thrash": lvl, "thrash_why": ", ".join(why), "mem_top": top,
              "kill_prefix": "" if h.get("local") else "ssh %s " % h["ssh"]})


def _status(v, h, th):
    level, reasons = "ok", []

    def bump(new, text):
        nonlocal level
        if new == "crit" or level == "ok":
            level = new
        reasons.append(text)

    # Linux load also counts tasks that wait for disk, so high load alone does not mean a busy CPU.
    if v["load_ratio"] >= th["load_crit"] and v["cpu"] >= th["cpu_crit"]:
        bump("crit", "CPU %.0f%% busy, load %.1f on %d cores" % (v["cpu"], v["load"][0], v["cores"]))
    elif v["load_ratio"] >= th["load_warn"] and v["cpu"] >= th["cpu_warn"]:
        bump("warn", "CPU %.0f%% busy, load %.1f on %d cores" % (v["cpu"], v["load"][0], v["cores"]))
    if (v["iowait"] or 0) >= th["iowait_warn"]:
        bump("warn", "CPU waits for disk %.0f%% of the time" % v["iowait"])
    free = 100 - v["mem_pct"]
    if free <= th["mem_free_crit"]:
        bump("crit", "memory %.0f%% used" % v["mem_pct"])
    elif free <= th["mem_free_warn"]:
        bump("warn", "memory %.0f%% used" % v["mem_pct"])
    for x in v["disks"]:
        if x["pct"] >= th["disk_crit"]:
            bump("crit", "disk %s %.0f%% full" % (x["mount"], x["pct"]))
        elif x["pct"] >= th["disk_warn"]:
            bump("warn", "disk %s %.0f%% full" % (x["mount"], x["pct"]))
    if v["thrash"] != "ok":
        bump(v["thrash"], "swap thrashing (%s)" % v["thrash_why"])
        reasons.insert(0, reasons.pop())
    if not v["reachable"]:
        bump("crit" if h.get("critical", True) else "warn", "unreachable: %s" % (v["error"] or "stale data")[:120])
        reasons.insert(0, reasons.pop())
    return level, reasons


def issues(hosts, add):
    for h in hosts:
        if h["status"] == "offline":
            if h["critical"]:
                add("crit", "machine", "%s is offline" % h["name"])
            continue
        if h["status"] == "down":
            add("crit" if h["critical"] else "info", "machine", "%s: no data (%s)" % (h["name"], (h.get("error") or "")[:100]))
            continue
        if h["status"] in ("warn", "crit"):
            who = fix = None
            if h["thrash"] != "ok" and h["mem_top"]:
                p = h["mem_top"][0]
                who = "%s%s (pid %d) holds %s" % (p["project"], " / " + p["sub"] if p.get("sub") else "", p["pid"], gb(p["rss"] + p.get("swap", 0)))
                fix = "%skill %d" % (h["kill_prefix"], p["pid"])
            elif h["hog"] and h["hog"]["cores"] >= 0.1 and any(r.startswith("CPU") for r in h["reasons"]):
                hg = h["hog"]
                who = "%s%s uses %.1f cores" % (hg["project"], " / " + hg["sub"] if hg["sub"] else "", hg["cores"])
            add(h["status"], "machine", "%s: %s" % (h["name"], "; ".join(h["reasons"])), who, fix)
        for j in h.get("jobs") or []:
            # A job that the memory cap stopped, or whose supervisor died, in the last 2 hours: tell its owner.
            if j.get("state") in ("killed", "lost") and time.time() - (j.get("ended") or 0) < 7200:
                add("warn", "machine", "%s: job %s (%s) %s" % (h["name"], j.get("name"), j.get("project"),
                                                              j.get("why") or ("was lost" if j["state"] == "lost" else "was killed")),
                    "owner thread %s" % j["owner"] if j.get("owner") else None, "job logs %s" % j["id"])
        if h.get("cores") and h["thrash"] == "ok" and (h["swap_pct"] or 0) >= 90 and h["mem_top"]:
            p = max(h["mem_top"], key=lambda x: x.get("swap", 0))
            if p.get("swap"):
                add("info", "machine", "%s: swap %.0f%% full, no thrashing now" % (h["name"], h["swap_pct"]),
                    "%s (pid %d) has %s in swap" % (p["project"], p["pid"], gb(p["swap"])))
