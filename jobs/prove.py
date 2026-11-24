#!/usr/bin/env python3
"""Proof test for the job tool on a Linux host. It fills the host on purpose with jobs that allocate real memory,
and measures: stalls outside the job slice (a probe that sleeps 50 ms), pauses, kills, lost work.
usage: prove.py SCENARIO"""
import json, os, subprocess, sys, threading, time
G = 2 ** 30
EAT = r'''
import sys, time
gb, hold, rate = float(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
a = []; t0 = time.time(); n = 0
while n < gb * 4:
    b = bytearray(256 * 2**20)
    for i in range(0, len(b), 4096): b[i] = 1
    a.append(b); n += 1
    if rate: time.sleep(0.25 / rate)
end = time.time() + hold; s = 0
while time.time() < end:
    for b in a:                        # touch every page: a job that works with all of its memory
        s += sum(b[::4096])
    time.sleep(0.2)
print("EAT_DONE", gb, round(time.time() - t0)); sys.stdout.flush()
'''
open("/tmp/eat.py", "w").write(EAT)

def mem():
    d = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in open("/proc/meminfo")}
    return d["MemTotal"], d["MemAvailable"], d["SwapFree"]

def psi(p="/proc/pressure/memory"):
    for l in open(p):
        if l.startswith("full"): return float(l.split("avg10=")[1].split()[0]), int(l.split("total=")[1])

def swp():
    d = dict(l.split() for l in open("/proc/vmstat"))
    return int(d["pswpin"]) * 4096 / G, int(d["pswpout"]) * 4096 / G

def run(name, hint, real, hold, rate=4, extra=()):
    r = subprocess.run(["job", "run", "--project", "prove", "--name", name, "--owner", "prove", "--peak-gb", str(hint), *extra,
                        "--", "python3", "/tmp/eat.py", str(real), str(hold), str(rate)], capture_output=True, text=True)
    return r.returncode, (r.stdout.strip().splitlines() or [""])[0], r.stderr.strip()

def jobs():
    return [m for m in json.loads(subprocess.run(["job", "ls", "--json"], capture_output=True, text=True).stdout)["jobs"] if m.get("project") == "prove"]

stall = {"max": 0.0, "over1s": 0, "n": 0}
samples = []
stop = False
def probe():
    while not stop:
        t = time.time(); time.sleep(0.05); d = time.time() - t - 0.05
        stall["n"] += 1; stall["max"] = max(stall["max"], d); stall["over1s"] += d > 1
def sample():
    while not stop:
        tot, av, sf = mem(); samples.append((time.time(), av / G, sf / G, psi()[0])); time.sleep(1)
for f in (probe, sample): threading.Thread(target=f, daemon=True).start()

tot, av0, sf0 = mem(); T, AV = tot / G, av0 / G
sc = sys.argv[1]; ids = []; rejected = 0; t0 = time.time(); psi0 = psi()[1]; sw0 = swp()
print("host RAM %.0f GB, available %.1f GB, swap free %.1f GB, scenario %s" % (T, AV, sf0 / G, sc)); sys.stdout.flush()
def start(name, hint, real, hold, rate=4, extra=()):
    global rejected
    rc, jid, err = run(name, hint, real, hold, rate, extra)
    if rc == 75: rejected += 1; print("  rejected:", name, "|", err[-170:])
    elif rc: print("  ERROR", rc, err[-200:])
    else: ids.append(jid); print("  started %s %s hint %g GB, real %g GB" % (name, jid, hint, real))
    sys.stdout.flush()
if sc == "burst":       # real use 1.6x the free memory, submitted 1 s apart
    for i in range(int(AV * 1.6 / 4) + 1): start("b%d" % i, 4, 4, 150, extra=("--queue",)); time.sleep(1)
elif sc == "lie":       # hint 1 GB, real 40% of the free memory each, 5 jobs: no start check can know
    for i in range(5): start("liar%d" % i, 1, round(AV * 0.4, 1), 120, rate=2, extra=("--queue",)); time.sleep(10)
elif sc == "stream":    # a long line of work: real use 3x the free memory in 3 GB jobs, hint 2 GB, one each 4 s
    for i in range(int(AV * 3 / 3)): start("s%d" % i, 2, 3, 90, extra=("--queue",)); time.sleep(4)
elif sc == "oversize":  # one job that wants 1.3x the RAM
    start("huge", 1, round(T * 1.3), 60, rate=8)
elif sc == "reject":    # fill the host with one job, then submit with no --queue
    start("fill", 1, round(AV - 4), 100, rate=8); time.sleep(50); start("late", 4, 1, 10)
deadline = time.time() + 1500; seen_paused = set()
while time.time() < deadline:
    js = {m["id"]: m for m in jobs() if m["id"] in ids}
    for m in js.values():
        if m.get("paused") or m.get("pauses"): seen_paused.add(m["id"])
    if js and all(m["state"] in ("done", "failed", "killed", "stopped", "lost") for m in js.values()): break
    time.sleep(3)
stop = True; time.sleep(1.2)
js = [m for m in jobs() if m["id"] in ids]
st = {}
for m in js: st[m["state"]] = st.get(m["state"], 0) + 1
print("RESULT scenario=%s jobs=%d states=%s rejected=%d paused_jobs=%d pauses=%d paused_seconds=%d seconds=%d" % (sc, len(js), st, rejected, len(seen_paused), sum(m.get("pauses") or 0 for m in js), sum(m.get("paused_s") or 0 for m in js), time.time() - t0))
print("RESULT min_available=%.1f GB min_swap_free=%.1f GB max_psi_full_avg10=%.1f%% host_stall_total=%.1f s" % (min(s[1] for s in samples), min(s[2] for s in samples), max(s[3] for s in samples), (psi()[1] - psi0) / 1e6))
print("RESULT probe outside the jobs: samples=%d max_delay=%.2f s delays_over_1s=%d" % (stall["n"], stall["max"], stall["over1s"]))
print("RESULT swap traffic: read %.1f GB, written %.1f GB" % (swp()[0] - sw0[0], swp()[1] - sw0[1]))
for m in js:
    if m["state"] != "done": print("  not done:", m["name"], m["state"], (m.get("why") or "")[:160])

