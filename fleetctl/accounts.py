"""Model accounts: collect them from the account sources, compute pace and run-out time for each,
and pool all accounts of one provider into one chart with a 7-day projection."""
import time

from .util import WEEK, rel, ts

FIELDS = ("provider", "id", "label", "plan", "weekly_used", "weekly_reset", "short_used", "short_reset", "status",
          "status_text", "active", "resets", "credits")


def normalize(a, source):
    """Check one account from a source and convert times to epoch seconds. See docs/plugins.md for the fields."""
    if not a.get("provider") or not a.get("id"):
        raise ValueError("account needs provider and id: %r" % {k: a.get(k) for k in ("provider", "id", "label")})
    out = {k: a.get(k) for k in FIELDS}
    out["label"] = out["label"] or out["id"]
    out["status"] = out["status"] or "ok"
    out["active"] = bool(out["active"])
    for k in ("weekly_reset", "short_reset"):
        out[k] = ts(out[k])
    for k in ("weekly_used", "short_used"):
        out[k] = None if out[k] is None else float(out[k])
    if out["resets"] is not None:
        out["resets"] = [{"label": r.get("label") or "reset", "expires": ts(r.get("expires")), "weekly": r.get("weekly", True),
                          "usable": r.get("usable", True)} for r in out["resets"]]
    out["source"] = source
    return out


def collect(store, sources, refresh):
    accounts, issues = [], []
    for name, src in sources:
        try:
            res = src.collect(refresh)
            accounts += [normalize(a, name) for a in res.get("accounts", [])]
            issues += res.get("issues", [])
            store.set_error("accounts:" + name, None)
        except Exception as e:
            store.set_error("accounts:" + name, e)
            with store.lock:
                accounts += [a for a in store.accounts if a["source"] == name]
                issues += [i for i in store.source_issues if i.get("source") == name]
            continue
    now = time.time()
    with store.lock:
        store.accounts = accounts
        store.source_issues = issues
        store.updated["accounts"] = now
        for a in accounts:
            if a["weekly_used"] is not None:
                store.acct_hist.append([int(now), a["provider"], a["id"], a["weekly_used"], a["weekly_reset"]])
        store.acct_hist = [x for x in store.acct_hist if x[0] > now - 8 * 86400]
    store.save_history()


def view(store, now):
    out = []
    for a in store.accounts:
        v = dict(a)
        used, reset = a["weekly_used"], a["weekly_reset"]
        if used is not None and reset:
            rate, src = _rate(store.acct_hist, a, now)
            v["rate"], v["rate_src"] = rate, src
            v["eta"] = now if used >= 100 else (now + (100 - used) / rate * 3600 if rate and rate > 0.01 else None)
            v["runs_out_before_reset"] = bool(v["eta"] and v["eta"] < reset and used < 100)
            v["past"] = _past(store.acct_hist, a, now)
        out.append(v)
    return out


def _rate(hist, a, now):
    """Weekly % per hour: from the history of this week (about the last 6 hours), else the week average."""
    used, reset = a["weekly_used"], a["weekly_reset"]
    same = [x for x in hist if x[2] == a["id"] and x[4] and abs(x[4] - reset) < 3600 and now - x[0] >= 3 * 3600]
    if same:
        newest = max(same, key=lambda x: x[0])
        ref = newest if now - newest[0] <= 8 * 3600 else min(same, key=lambda x: abs((now - x[0]) - 6 * 3600))
        dt = (now - ref[0]) / 3600
        return round(max(0.0, (used - ref[3]) / dt), 2), "last %.0fh" % dt
    start = reset - WEEK
    if now - start > 3600:
        return round(used / ((now - start) / 3600), 2), "week avg"
    return None, None


def _past(hist, a, now):
    """Weekly % used over the last 7 days. Inserts the drop to 0 at each reset."""
    lo = now - WEEK
    pts = sorted((x for x in hist if x[2] == a["id"] and x[3] is not None and x[4] and x[0] >= lo), key=lambda x: x[0])
    groups = []
    for x in pts:
        if groups and abs(groups[-1][0] - x[4]) < 3600:
            groups[-1][1].append([x[0], x[3]])
        else:
            groups.append([x[4], [[x[0], x[3]]]])
    if not groups or abs(groups[-1][0] - a["weekly_reset"]) >= 3600:
        groups.append([a["weekly_reset"], []])
    groups[-1][1].append([now, a["weekly_used"]])
    past = []
    for i, (r, gp) in enumerate(groups):
        start = r - WEEK
        # Before the first real sample of a week, assume use started at 0 at the week start. Keep this point even
        # when the week started before the window, so the line still reaches the left edge.
        if gp[0][0] > start:
            gp.insert(0, [start, 0])
        if past and gp[0][0] > past[-1][0]:
            past.append([gp[0][0], past[-1][1]])
        past.extend(gp)
        if i + 1 < len(groups) and r < groups[i + 1][1][0][0]:
            past.append([r, past[-1][1]])
    first = next((k for k, (t, _) in enumerate(past) if t >= lo), len(past))
    return [[int(t), round(u, 1)] for t, u in past[max(0, first - 1):]]


def interp(pts, t):
    if not pts or t < pts[0][0] or t > pts[-1][0]:
        return None
    for j in range(1, len(pts)):
        if pts[j][0] >= t:
            a, b = pts[j - 1], pts[j]
            return b[1] if b[0] == a[0] else a[1] + (b[1] - a[1]) * (t - a[0]) / (b[0] - a[0])
    return pts[-1][1]


def pools(accounts, providers, now):
    """One pool per provider. Values are % of the whole pool left. Each account is a band sized by its plan weight.
    Projection: the pool drains at the recent total rate (or this week's average, if higher), from the account
    with the closest reset first, as a router that burns the soonest-resetting quota first. A reset puts back that
    account's share."""
    out = {}
    for prov, meta in providers.items():
        acc = [a for a in accounts if a["provider"] == prov and a.get("weekly_used") is not None and a.get("weekly_reset")]
        if not acc:
            continue
        pw = meta.get("plan_weights") or {}
        W = {a["id"]: float(pw.get((a.get("plan") or "").lower(), 1)) for a in acc}
        tot = sum(W.values())
        h0 = int(now // 3600) * 3600
        times = [h0 - 3600 * i for i in range(168, 0, -1)] + [int(now)]
        bands = {a["id"]: [] for a in acc}
        for t in times:
            for a in acc:
                u = interp(a.get("past"), t)
                bands[a["id"]].append(None if u is None else round(W[a["id"]] * (100 - min(100, u)) / tot, 2))
        recent = sum(W[a["id"]] * (a.get("rate") or 0) / 100 for a in acc)
        avg = 0.0
        for a in acc:
            el = (now - (a["weekly_reset"] - WEEK)) / 3600
            if el > 12:
                avg += W[a["id"]] * min(100, a["weekly_used"]) / el / 100
        demand = max(recent, avg)  # plan units per hour
        left = {a["id"]: W[a["id"]] * (100 - min(100, a["weekly_used"])) / 100 for a in acc}
        nxt = {a["id"]: a["weekly_reset"] for a in acc}
        resets, prev = [], now
        for i in range(1, 169):
            t = h0 + 3600 * i
            need = demand * (t - prev) / 3600
            for e in sorted(left, key=lambda e: nxt[e]):
                take = min(left[e], need)
                left[e] -= take
                need -= take
            for e in left:
                while nxt[e] <= t:
                    resets.append([int(nxt[e]), e, round(100 * W[e] / tot, 1)])
                    left[e] = W[e]
                    nxt[e] += WEEK
            times.append(t)
            for e in left:
                bands[e].append(round(100 * left[e] / tot, 2))
            prev = t
        total = [None if any(bands[e][k] is None for e in bands) else round(sum(bands[e][k] for e in bands), 2) for k in range(len(times))]
        dry, start = [], None
        for k in range(168, len(times)):
            if total[k] is not None and total[k] < 0.5:
                start = start or times[k]
            elif start:
                dry.append([start, times[k]])
                start = None
        if start:
            dry.append([start, None])
        out[prov] = {"times": times, "now_idx": 168, "bands": bands, "total": total, "weights": W, "units": tot,
                     "demand_pct_day": round(100 * demand * 24 / tot, 1), "resets": resets, "dry": dry, "left_now": total[168]}
    return out


def issues(accounts, providers, source_issues, now, add):
    for a in accounts:
        name = "%s %s" % (providers.get(a["provider"], {}).get("name", a["provider"]), a["label"])
        if a["status"] == "signed_out":
            add("crit", "account", "%s is signed out%s" % (name, ": " + a["status_text"] if a.get("status_text") else ""))
        elif a["status"] == "error":
            add("warn", "account", "%s: %s" % (name, a.get("status_text") or "error"))
        elif a["status"] == "no_quota":
            add("warn", "account", "%s: quota read failed" % name)
        if a.get("weekly_used") is not None and a["weekly_used"] >= 100:
            add("info", "account", "%s is at its weekly limit" % name)
        elif a.get("runs_out_before_reset") and a.get("eta"):
            lvl = "warn" if a["eta"] - now < 2 * 86400 else "info"
            add(lvl, "account", "%s will run out %s, before its reset %s" % (name, rel(a["eta"] - now), rel(a["weekly_reset"] - now)))
        for r in a.get("resets") or []:
            if r.get("expires") and 0 < r["expires"] - now < 3 * 86400:
                add("info", "account", "%s: a reset expires %s" % (name, rel(r["expires"] - now)))
    for i in source_issues:
        add(i.get("level", "warn"), i.get("area", "account"), i["text"], i.get("who"), i.get("fix"))
