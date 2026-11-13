#!/usr/bin/env python3
"""Fleet Control host probe. Prints one JSON object with load, memory, disks, GPUs and the
busiest processes (with their working directory). Runs on Linux and macOS with only the
standard library. Run it locally, or with: ssh HOST python3 - < host_probe.py"""
import json, os, socket, subprocess, sys, time, platform


def sh(cmd, timeout=6):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


SKIP_MOUNTS = ("/System/Volumes/VM", "/System/Volumes/Preboot", "/System/Volumes/Update", "/System/Volumes/xarts",
               "/System/Volumes/iSCPreboot", "/System/Volumes/Hardware", "/boot", "/snap")


def disks():
    out, seen = [], set()
    import re
    for ln in sh("df -kP 2>/dev/null").splitlines()[1:]:
        m = re.match(r"^(.*?)\s+(\d+)\s+(\d+)\s+(\d+)\s+\d+%\s+(.*)$", ln)
        if not m:
            continue
        dev, total, avail, mnt = m.group(1), int(m.group(2)), int(m.group(4)), m.group(5)
        if total < 15 * 1024 * 1024 or not dev.startswith("/dev/") or mnt.startswith(SKIP_MOUNTS):
            continue
        key = (total, avail)
        if key in seen:
            continue
        seen.add(key)
        out.append({"mount": mnt, "total": total * 1024, "used": (total - avail) * 1024})
    return out


def gpus():
    raw = sh("command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits", 5)
    res = []
    for ln in raw.strip().splitlines():
        p = [x.strip() for x in ln.split(",")]
        if len(p) == 4:
            try:
                res.append({"name": p[0], "util": float(p[1]), "mem_used": float(p[2]) * 1048576, "mem_total": float(p[3]) * 1048576})
            except ValueError:
                pass
    return res


def linux():
    import pwd
    hz = os.sysconf("SC_CLK_TCK")

    def vmstat():
        v = {}
        try:
            for l in open("/proc/vmstat"):
                k, n = l.split()
                if k in ("pswpin", "pswpout", "pgmajfault"):
                    v[k] = int(n)
        except Exception:
            pass
        return v

    def cpu_times():
        with open("/proc/stat") as f:
            v = [int(x) for x in f.readline().split()[1:]]
        return sum(v), v[3] + (v[4] if len(v) > 4 else 0), (v[4] if len(v) > 4 else 0)

    def proc_ticks():
        r = {}
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % d) as f:
                    s = f.read()
                rest = s[s.rfind(")") + 2:].split()
                r[int(d)] = (int(rest[11]) + int(rest[12]), int(rest[19]), s[s.find("(") + 1:s.rfind(")")])
            except Exception:
                pass
        return r

    t0, i0, w0io = cpu_times(); p0 = proc_ticks(); v0 = vmstat(); w0 = time.time()
    time.sleep(1.0)
    t1, i1, w1io = cpu_times(); p1 = proc_ticks(); v1 = vmstat(); dt = time.time() - w0
    util = 100.0 * (1 - (i1 - i0) / max(1, (t1 - t0)))
    iowait = 100.0 * (w1io - w0io) / max(1, (t1 - t0))
    boot = time.time() - float(open("/proc/uptime").read().split()[0])
    procs = []
    for pid, (ticks, start, comm) in p1.items():
        prev = p0.get(pid)
        procs.append([100.0 * (ticks - prev[0]) / hz / dt if prev else 0.0, pid, comm, start])
    procs.sort(reverse=True)
    page = os.sysconf("SC_PAGE_SIZE")
    res = []
    for cpu, pid, comm, start in procs[:60]:
        if cpu < 0.5 and len(res) >= 12:
            break
        try:
            cmd = open("/proc/%d/cmdline" % pid, "rb").read().replace(b"\0", b" ").decode("utf-8", "replace").strip() or comm
        except Exception:
            cmd = comm
        try:
            cwd = os.readlink("/proc/%d/cwd" % pid)
        except Exception:
            cwd = ""
        try:
            rss = int(open("/proc/%d/statm" % pid).read().split()[1]) * page
        except Exception:
            rss = 0
        try:
            uid = [l for l in open("/proc/%d/status" % pid) if l.startswith("Uid:")][0].split()[1]
            user = pwd.getpwuid(int(uid)).pw_name
        except Exception:
            user = ""
        res.append({"pid": pid, "cpu": round(cpu, 1), "rss": rss, "comm": comm, "cmd": cmd[:300], "cwd": cwd,
                    "user": user, "age": int(time.time() - (boot + start / hz))})
    mem = {}
    for l in open("/proc/meminfo"):
        k, v = l.split(":", 1)
        mem[k] = int(v.split()[0]) * 1024
    psi = {}
    try:
        for l in open("/proc/pressure/memory"):
            kind, rest = l.split(None, 1)
            psi[kind] = float(rest.split()[0].split("=")[1])
    except Exception:
        pass
    # Biggest memory users (RSS + swapped-out pages), so a thrashing machine shows who to stop.
    memtop = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            rss = swp = 0
            name = ""
            for l in open("/proc/%s/status" % d):
                if l.startswith("Name:"):
                    name = l.split(None, 1)[1].strip()
                elif l.startswith("VmRSS:"):
                    rss = int(l.split()[1]) * 1024
                elif l.startswith("VmSwap:"):
                    swp = int(l.split()[1]) * 1024
            if rss + swp > 200 * 1048576:
                memtop.append([rss + swp, int(d), name, rss, swp])
        except Exception:
            pass
    memtop.sort(reverse=True)
    mtop = []
    for _, pid, name, rss, swp in memtop[:8]:
        try:
            cmd = open("/proc/%d/cmdline" % pid, "rb").read().replace(b"\0", b" ").decode("utf-8", "replace").strip() or name
        except Exception:
            cmd = name
        try:
            cwd = os.readlink("/proc/%d/cwd" % pid)
        except Exception:
            cwd = ""
        mtop.append({"pid": pid, "comm": name, "rss": rss, "swap": swp, "cmd": cmd[:300], "cwd": cwd})
    return {"cpu_util": round(util, 1), "iowait": round(iowait, 1), "mem_total": mem.get("MemTotal", 0), "mem_avail": mem.get("MemAvailable", 0),
            "swap_total": mem.get("SwapTotal", 0), "swap_used": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
            "swap_counters": {"in": v1.get("pswpin", 0), "out": v1.get("pswpout", 0), "majflt": v1.get("pgmajfault", 0)},
            "swap_rate_1s": {"in": (v1.get("pswpin", 0) - v0.get("pswpin", 0)) / dt, "out": (v1.get("pswpout", 0) - v0.get("pswpout", 0)) / dt},
            "page_size": page,
            "psi": psi, "mem_top": mtop, "procs": res, "uptime": int(time.time() - boot)}


def mac():
    def swapins():
        v = {}
        for l in sh("vm_stat").splitlines():
            if l.startswith("Swapins:"):
                v["in"] = int(l.split()[-1].rstrip("."))
            elif l.startswith("Swapouts:"):
                v["out"] = int(l.split()[-1].rstrip("."))
        return v

    util = 0.0
    s0 = swapins(); w0 = time.time()
    # iostat samples CPU over 1 s and costs almost nothing ("top -l 2" costs about 1 s of CPU, which a loaded Mac lacks).
    io = sh("iostat -n 0 -c 2 -w 1", 10).strip().splitlines()
    s1 = swapins(); dt = max(0.5, time.time() - w0)
    try:
        util = 100 - float(io[-1].split()[2])
    except Exception:
        pass
    total = int(sh("sysctl -n hw.memsize").strip() or 0)
    pg, vals = 16384, {}
    for l in sh("vm_stat").splitlines():
        if "page size of" in l:
            try:
                pg = int(l.split("page size of")[1].split()[0])
            except Exception:
                pass
        if ":" in l:
            k, v = l.split(":", 1)
            try:
                vals[k.strip()] = int(v.strip().rstrip("."))
            except ValueError:
                pass
    # Same as Activity Monitor: used = app memory (anonymous - purgeable) + wired + compressed.
    # Inactive pages are not free on macOS: most hold app data that must be compressed or swapped out first.
    used = (vals.get("Anonymous pages", 0) - vals.get("Pages purgeable", 0) + vals.get("Pages wired down", 0)
            + vals.get("Pages occupied by compressor", 0)) * pg
    avail = max(0, total - used) if used > 0 else (vals.get("Pages free", 0) + vals.get("Pages speculative", 0)) * pg
    ps = []
    for r in sh("ps -Ao pid=,pcpu=,rss=,etime=,user=,comm=", 6).splitlines():
        p = r.split(None, 5)
        if len(p) < 6:
            continue
        try:
            ps.append([float(p[1]), int(p[0]), int(p[2]) * 1024, p[3], p[4], p[5]])
        except ValueError:
            pass
    ps.sort(reverse=True)
    sel = [x for x in ps[:60] if x[0] >= 0.5][:45] or ps[:12]
    pids = ",".join(str(x[1]) for x in sel)
    cwd, cur = {}, None
    for l in sh("lsof -a -d cwd -Fpn -p %s 2>/dev/null" % pids, 8).splitlines():
        if l.startswith("p"):
            cur = int(l[1:])
        elif l.startswith("n") and cur:
            cwd[cur] = l[1:]
    args = {}
    for l in sh("ps -o pid=,args= -p %s" % pids, 6).splitlines():
        p = l.strip().split(None, 1)
        if len(p) == 2:
            args[int(p[0])] = p[1]

    def etime(s):
        d = 0
        if "-" in s:
            dd, s = s.split("-", 1)
            d = int(dd) * 86400
        parts = [int(x) for x in s.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return d + parts[0] * 3600 + parts[1] * 60 + parts[2]

    res = []
    for cpu, pid, rss, et, user, comm in sel:
        try:
            age = etime(et)
        except Exception:
            age = 0
        res.append({"pid": pid, "cpu": cpu, "rss": rss, "comm": os.path.basename(comm), "cmd": args.get(pid, comm)[:300],
                    "cwd": cwd.get(pid, ""), "user": user, "age": age})
    try:
        up = int(time.time() - int(sh("sysctl -n kern.boottime").split("sec =")[1].split(",")[0]))
    except Exception:
        up = 0
    swap_total = swap_used = 0
    su = sh("sysctl -n vm.swapusage")  # total = 2048.00M  used = 1069.25M  free = 978.75M
    try:
        f = su.replace("=", " ").split()
        unit = lambda s: float(s[:-1]) * {"K": 1024, "M": 1048576, "G": 1073741824}[s[-1]]
        swap_total, swap_used = unit(f[f.index("total") + 1]), unit(f[f.index("used") + 1])
    except Exception:
        pass
    try:
        level = int(sh("sysctl -n kern.memorystatus_level").strip())
    except Exception:
        level = None
    mtop = []
    for cpu, pid, rss, et, user, comm in sorted(ps, key=lambda x: -x[2])[:8]:
        mtop.append({"pid": pid, "comm": os.path.basename(comm), "rss": rss, "swap": 0, "cmd": comm[:300], "cwd": ""})
    return {"cpu_util": round(util, 1), "mem_total": total, "mem_avail": avail, "swap_total": swap_total, "swap_used": swap_used,
            "swap_counters": {"in": s1.get("in", 0), "out": s1.get("out", 0)},
            "swap_rate_1s": {"in": (s1.get("in", 0) - s0.get("in", 0)) / dt, "out": (s1.get("out", 0) - s0.get("out", 0)) / dt},
            "mem_free_level": level, "page_size": pg, "mem_top": mtop, "procs": res, "uptime": up}


def agentjobs():
    """Jobs started with 'job run' (jobs/job.py): running and queued ones, and ones that ended in the last 6 hours."""
    base = os.path.expanduser("~/.agentjobs")
    out = []
    try:
        names = os.listdir(base)
    except OSError:
        return out
    cut = time.time() - 6 * 3600
    for n in names:
        try:
            m = json.load(open(os.path.join(base, n, "meta.json")))
        except Exception:
            continue
        if m.get("state") in ("done", "failed", "killed", "stopped", "lost") and (m.get("ended") or 0) < cut:
            continue
        mem = m.get("mem_now") or 0
        if m.get("unit") and m.get("state") == "run":
            try:
                mem = int(open("/sys/fs/cgroup/agentjobs.slice/%s/%s.service/memory.current" % (m["slice"], m["unit"])).read())
            except Exception:
                pass
        e = {k: m.get(k) for k in ("id", "name", "project", "owner", "state", "created", "started", "ended", "exit",
                                   "signal", "why", "peak_gb", "mem_gb", "peak_bytes", "passes")}
        e.update(mem_now=mem, cmd=" ".join(m.get("cmd") or [])[:300])
        out.append(e)
    return sorted(out, key=lambda m: m.get("created") or 0)[-30:]


def main():
    t = time.time()
    data = {"ok": True, "hostname": socket.gethostname(), "os": platform.system(), "cores": os.cpu_count(), "load": list(os.getloadavg())}
    data.update(mac() if sys.platform == "darwin" else linux())
    data["disks"] = disks()
    data["gpus"] = gpus()
    data["jobs"] = agentjobs()
    data["ts"] = time.time()
    data["probe_ms"] = int((time.time() - t) * 1000)
    print(json.dumps(data))


if __name__ == "__main__":
    main()
