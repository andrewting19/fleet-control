"""Token use: merge the agent scans of all hosts into hourly series per project and provider,
and into threads (a top session plus its subagents). Cost is tokens x list price ("$ weight")."""
from .models import norm
from .util import WEEK, run_probe

WINDOWS = (("1h", 3600), ("24h", 86400), ("7d", WEEK))


def scan(store, host):
    try:
        d = run_probe(host, "agent_scan.py", 300)
    except Exception as e:
        store.set_error("scan:" + host["name"], e)
        return
    with store.lock:
        store.scans[host["name"]] = d
    store.set_error("scan:" + host["name"], None)


def _bucket():
    # [provider, cost, tokens, uncached input, cache read, cache write, output, requests]
    return [None, 0.0, 0, 0, 0, 0, 0, 0]


def view(store, models, projects, titles, now):
    hour_now = int(now // 3600) * 3600
    hours = [hour_now - 3600 * i for i in range(167, -1, -1)]
    hidx = {h: i for i, h in enumerate(hours)}
    series, by_host, by_model, proj_models, threads = {}, {}, {}, {}, {}
    index = {s["sid"]: (host, s) for host, sc in store.scans.items() for s in sc.get("sessions", [])}
    for host, sc in store.scans.items():
        for s in sc.get("sessions", []):
            proj, sub = projects(s.get("cwd"))
            th = _thread(threads, index, titles, host, s, proj, sub)
            for hstr, per_model in s["bk"].items():
                h, age = int(hstr), now - int(hstr)
                for m, v in per_model.items():
                    prov, c, tok, nm = models.provider(m), models.cost(m, v), v[0] + v[1] + v[2] + v[3], norm(m)
                    i = hidx.get(h)
                    if i is not None:
                        for tgt in (series.setdefault(proj, {}), by_host.setdefault(host, {})):
                            ps = tgt.setdefault(prov, {"cost": [0.0] * 168, "tok": [0] * 168})
                            ps["cost"][i] += c
                            ps["tok"][i] += tok
                    for wn, ws in WINDOWS:
                        if age >= ws:
                            continue
                        w = th["w"].setdefault(wn, {"cost": 0.0, "tok": 0, "req": 0, "by_prov": {}, "by_model": {}})
                        w["cost"] += c
                        w["tok"] += tok
                        w["req"] += v[4]
                        w["by_prov"][prov] = w["by_prov"].get(prov, 0) + c
                        for b in (w["by_model"].setdefault(nm, _bucket()), proj_models.setdefault(wn, {}).setdefault(proj, {}).setdefault(nm, _bucket())):
                            b[0] = prov
                            b[1] += c
                            b[2] += tok
                            for k in range(5):
                                b[3 + k] += v[k]
                        mm = by_model.setdefault(wn, {}).setdefault(nm, {"cost": 0.0, "tok": 0, "prov": prov, "cached": 0})
                        mm["cost"] += c
                        mm["tok"] += tok
                        mm["cached"] += v[1]
    tlist, subs = {}, {}
    for wn, _ in WINDOWS:
        arr = sorted((t for t in threads.values() if wn in t["w"]), key=lambda t: -t["w"][wn]["cost"])
        tlist[wn] = [{"title": t["title"], "project": t["project"], "sub": t["sub"], "host": ", ".join(sorted(t["hosts"])),
                      "client": t["client"], "sessions": len(t["sessions"]), "last": t["last"], **t["w"][wn]} for t in arr[:60]]
        for t in threads.values():
            if wn in t["w"]:
                d = subs.setdefault(wn, {}).setdefault(t["project"], {})
                d[t["sub"] or "-"] = d.get(t["sub"] or "-", 0) + t["w"][wn]["cost"]
    active = {host: sum(1 for s in sc.get("sessions", []) if s.get("last") and now - s["last"] < 900) for host, sc in store.scans.items()}
    return {"hours": hours, "series": series, "by_host": by_host, "threads": tlist, "models": by_model, "proj_models": proj_models,
            "subs": subs, "active_sessions": active, "scanned": {h: sc.get("ts") for h, sc in store.scans.items()}}


def _thread(threads, index, titles, host, s, proj, sub):
    """Subagent sessions count toward their top parent. A title source may group sessions further (one app thread)."""
    root, hops = s["sid"], 0
    while hops < 8:
        par = index.get(root, (None, {}))[1].get("parent")
        if not par or par not in index:
            break
        root, hops = par, hops + 1
    t = titles.get(root) or titles.get(s["sid"])
    if t and t.get("group"):
        key, title, client = "g:" + t["group"], t.get("group_title") or t.get("title"), t.get("client") or "?"
    else:
        rs = index.get(root, (None, s))[1]
        key = host + ":" + root
        title = (t or {}).get("title") or rs.get("title") or "(untitled)"
        client = {"codex": rs.get("orig") or "Codex", "claude": "Claude Code"}.get(rs.get("src"), rs.get("src") or "?")
    th = threads.setdefault(key, {"title": title, "project": proj, "sub": sub, "client": client, "sessions": set(),
                                  "w": {}, "last": 0, "hosts": set()})
    th["sessions"].add(s["sid"])
    th["hosts"].add(host)
    th["last"] = max(th["last"], s.get("last") or 0)
    return th
