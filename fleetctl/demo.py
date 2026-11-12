"""Demo mode: realistic made-up machines, accounts and token use, so you can see the page with no setup
(python3 fleet.py serve --demo). The data goes through the same code as real data."""
import math
import random
import time

from .util import WEEK

GB = 1 << 30
HOSTS = [
    {"name": "laptop", "local": True, "scan": True, "os": "macos", "cores": 12, "mem": 36, "cpu": 22, "memu": 71, "swap": (4, 1.1), "disk": [("/", 994, 61)]},
    {"name": "gpu-box", "ssh": "gpu-box", "scan": True, "os": "linux", "cores": 32, "mem": 128, "cpu": 88, "memu": 64, "swap": (8, 0.4),
     "disk": [("/", 1832, 72), ("/data", 3726, 91)], "gpu": ("RTX 4090", 97)},
    {"name": "build-server", "ssh": "build", "scan": True, "os": "linux", "cores": 16, "mem": 32, "cpu": 41, "memu": 93, "swap": (8, 7.8),
     "disk": [("/", 931, 48)], "thrash": True},
    {"name": "gaming-pc", "ssh": "gaming-pc", "os": "windows", "cores": 16, "mem": 64, "cpu": 9, "memu": 38, "swap": (16, 0.9),
     "disk": [("C:", 1863, 64), ("D:", 3726, 97)]},
    {"name": "vps-1", "ssh": "vps-1", "os": "linux", "cores": 2, "mem": 4, "cpu": 6, "memu": 52, "swap": (2, 0.2), "disk": [("/", 80, 44)], "hidden": True},
    {"name": "vps-2", "ssh": "vps-2", "os": "linux", "cores": 2, "mem": 4, "cpu": 3, "memu": 33, "swap": (0, 0), "disk": [("/", 80, 21)], "hidden": True},
]
PROJECTS = {
    "api-server": ["auth", "billing", "search"], "web-app": ["checkout", "dashboard"], "ml-pipeline": ["training", "eval"],
    "mobile": [], "infra": ["terraform"], "docs-site": [],
}
TITLES = ["Fix flaky checkout test", "Migrate auth to OAuth 2.1", "Speed up search indexing", "Train ranking model v3",
          "Eval harness for ranker", "Dark mode for dashboard", "Terraform: move to new region", "Write API reference",
          "Debug memory leak in worker", "Billing: proration edge cases", "iOS push notifications", "Refactor data loaders",
          "Upgrade to React 20", "Rate limiter for public API", "Nightly backup job", "Investigate p99 latency"]
MODELS = {"openai": ["gpt-6.1-sol", "gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna"], "anthropic": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2"]}
ACCOUNTS = [  # provider, label, plan, reset in hours, used now, rate %/h
    ("openai", "work@example.com", "pro", 30, 74, 1.4), ("openai", "personal@example.com", "plus", 100, 38, 0.6),
    ("anthropic", "work@example.com", "max_20x", 52, 55, 0.9), ("anthropic", "team@example.com", "max_5x", 8, 93, 1.8),
    ("anthropic", "personal@example.com", "pro", 140, 6, 0.3),
]


def fill(app):
    rnd = random.Random(7)
    now = time.time()
    app.cfg["hosts"] = [{k: h[k] for k in ("name", "local", "ssh", "scan", "os", "hidden") if k in h} for h in HOSTS]
    app.cfg["ui"]["project_colors"] = {}
    st = app.store
    st.acct_hist = []
    for prov, label, plan, reset_h, used, rate in ACCOUNTS:
        aid = "%s:%s" % (prov, label)
        reset = now + reset_h * 3600
        for k in range(7 * 24, -1, -1):  # one point per hour for the last week
            t = now - k * 3600
            r = reset - WEEK * math.floor((reset - t) / WEEK)  # the reset that ends the window of time t
            into = (t - (r - WEEK)) / 3600
            u = used * into / max(1, (now - (reset - WEEK)) / 3600) if r == reset else min(100.0, into * rate * 1.1)
            st.acct_hist.append([int(t), prov, aid, round(u + rnd.uniform(-0.3, 0.3), 1), r])
        st.accounts.append({"provider": prov, "id": aid, "label": label, "plan": plan, "weekly_used": used, "weekly_reset": reset,
                            "short_used": rnd.randint(5, 60), "short_reset": now + rnd.randint(1, 5) * 3600, "status": "ok",
                            "status_text": None, "active": label == "work@example.com", "credits": None, "source": "demo",
                            "resets": [{"label": "Weekly reset", "expires": now + 4 * 86400, "weekly": True, "usable": True}] if label.startswith("team") else []})
    sessions = {h["name"]: [] for h in HOSTS if h.get("scan")}
    for i, title in enumerate(TITLES):
        proj = list(PROJECTS)[i % len(PROJECTS)]
        subs = PROJECTS[proj]
        host = ["laptop", "gpu-box", "build-server"][i % 3]
        prov = "openai" if (i * 7) % 3 == 0 else "anthropic"
        weight = 3.0 / (1 + i * 0.35)
        bk = {}
        start = now - rnd.uniform(2, 160) * 3600
        for t in range(int(start // 3600) * 3600, int(now), 3600):
            if rnd.random() < 0.45:
                continue
            m = rnd.choice(MODELS[prov])
            base = weight * rnd.uniform(0.2, 1.0) * 2e6
            bk[str(t)] = {m: [int(base * 0.08), int(base * 0.86), int(base * 0.03), int(base * 0.03), rnd.randint(20, 200)]}
        sessions[host].append({"src": "codex" if prov == "openai" else "claude", "sid": "demo-%d" % i, "title": title, "orig": "Codex",
                               "cwd": "/home/dev/src/%s%s" % (proj, ("/" + subs[i % len(subs)]) if subs else ""),
                               "last": now - rnd.uniform(0, 3) * 3600, "bk": bk})
    st.scans = {h: {"ts": now, "sessions": s} for h, s in sessions.items()}
    app.cfg["projects"] = [{"match": r"/src/(?P<p>[^/]+)(?:/(?P<s>[^/]+))?"}] + app.cfg["projects"]
    from .projects import Projects
    app.projects = Projects(app.cfg["projects"])
    tick(app, rnd)


def tick(app, rnd=None):
    rnd = rnd or random.Random()
    now = time.time()
    st = app.store
    for h in HOSTS:
        cpu = max(1, min(100, h["cpu"] + rnd.uniform(-6, 6)))
        mem_total = h["mem"] * GB
        tot_swap, used_swap = h["swap"]
        procs = []
        for j, proj in enumerate(list(PROJECTS)[:4]):
            share = cpu * h["cores"] / (2 ** (j + 1))
            procs.append({"pid": 4000 + j * 17, "cpu": round(share, 1), "rss": int(mem_total * 0.05 / (j + 1)), "comm": ["python", "node", "cargo", "java"][j],
                          "cmd": "%s worker --project %s" % (["python", "node", "cargo", "java"][j], proj), "cwd": "/home/dev/src/" + proj, "user": "dev", "age": 3600 * (j + 1)})
        d = {"ok": True, "hostname": h["name"], "os": h["os"], "cores": h["cores"], "at": now,
             "load": [round(cpu / 100 * h["cores"] * rnd.uniform(0.95, 1.1), 2)] * 3, "cpu_util": round(cpu, 1),
             "iowait": None if h["os"] != "linux" else round(rnd.uniform(0, 4), 1),
             "mem_total": mem_total, "mem_avail": int(mem_total * (100 - h["memu"]) / 100),
             "swap_total": tot_swap * GB, "swap_used": int(used_swap * GB),
             "swap_in_mbps": rnd.uniform(3, 6) if h.get("thrash") else 0.0,
             "psi": {"some": 24.0, "full": 3.0} if h.get("thrash") else ({"some": 0.1, "full": 0.0} if h["os"] == "linux" else {}),
             "disks": [{"mount": m, "total": t * GB, "used": int(t * GB * p / 100)} for m, t, p in h["disk"]],
             "gpus": [{"name": h["gpu"][0], "util": h["gpu"][1], "mem_used": 20 * GB, "mem_total": 24 * GB}] if h.get("gpu") else [],
             "procs": procs, "mem_top": [dict(p, swap=int(GB * 2.5) if h.get("thrash") and k == 0 else 0) for k, p in enumerate(procs)],
             "uptime": 86400 * 9}
        st.hosts[h["name"]] = d
        hist = st.load_hist.setdefault(h["name"], [])
        hist.append([int(now), d["load"][0], d["cpu_util"], h["memu"], d["swap_in_mbps"], d["psi"].get("some")])
        del hist[:-720]
