#!/usr/bin/env python3
"""job: run a heavy command as a tracked job with a memory cap and memory admission.

  job run [--project P] [--name N] [--peak-gb G] [--mem-gb M] [--owner ID] -- COMMAND ARG...
  job ls [--json] [--all]        jobs of this user on this machine
  job logs ID [-n 50] [-f]
  job wait ID [--timeout S]      exit 0 ended, 1 still running at timeout, 2 missing or lost
  job stop ID
  sudo job install [--high-gb G --max-gb M]   Linux: create agentjobs.slice, install /usr/local/bin/job

Why: agents start batch jobs with nohup or '&' in an SSH shell. The job then has no owner, no memory limit, and no
queue, and several of them can push a machine into swap thrashing. Each job here:
  - outlives the shell (Linux: a systemd unit; macOS/Windows: a detached supervisor),
  - waits until the machine has --peak-gb of free memory (admission; first come first served, with backfill),
  - is stopped at --mem-gb (Linux: cgroup MemoryMax; Windows: Job Object; macOS: RSS watchdog),
  - runs at low CPU priority, and is killed before services when memory runs out (oom_score_adj 500),
  - records owner (CODEX_THREAD_ID or --owner), project, command, exit code and peak memory in ~/.agentjobs/ID/.
Linux jobs go to agentjobs-<project>.slice under agentjobs.slice; 'job install' caps that parent slice, so all
agent jobs together cannot take the memory of services. Admission also respects the reservations of other job
tools that list them in /etc/agentjobs.json: {"reservation_dirs": ["/dev/shm/x"]}, one JSON file for each job with
"state" ("run"), "pid", "peak_gb" and "scopes" (cgroup paths).
"""
import argparse, json, os, platform, re, shlex, signal, subprocess, sys, time, uuid
from pathlib import Path

SYS = platform.system()
GB = 2 ** 30
STATE = Path(os.environ.get("AGENTJOBS_DIR") or (Path.home() / ".agentjobs"))
SELF = os.path.realpath(__file__)
MARGIN_GB = float(os.environ.get("AGENTJOBS_MARGIN_GB", "6" if SYS == "Linux" else "4"))
CG = Path("/sys/fs/cgroup")
PARENT = "agentjobs.slice"
LOCK = "/dev/shm/agentjobs.lock" if SYS == "Linux" else str(STATE / ".lock")
CONF = Path("/etc/agentjobs.json")
MAX_PASSES = 8
POLL = 10
ENDED = ("done", "failed", "killed", "stopped", "lost")


def now():
    return time.time()


def slug(s, n=40):
    return re.sub(r"[^A-Za-z0-9_.]+", "_", s or "").strip("_.")[:n] or "misc"


def meta_path(jid):
    return STATE / jid / "meta.json"


def read(jid):
    try:
        return json.loads(meta_path(jid).read_text())
    except (OSError, ValueError):
        return None


def write(m):
    p = meta_path(m["id"])
    tmp = p.with_suffix(".tmp%d" % os.getpid())
    tmp.write_text(json.dumps(m, indent=1))
    os.replace(str(tmp), str(p))


def update(jid, **kw):
    m = read(jid) or {"id": jid}
    m.update(kw)
    write(m)
    return m


class Lock:
    def __enter__(self):
        self.f = open(LOCK, "a+")
        try:
            os.chmod(LOCK, 0o666)
        except OSError:
            pass
        if SYS == "Windows":
            import msvcrt
            while True:
                try:
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.2)
        else:
            import fcntl
            fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        if SYS == "Windows":
            import msvcrt
            try:
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        self.f.close()


# ---------------------------------------------------------------- memory
def cg_int(path):
    try:
        v = Path(path).read_text().strip()
        return None if v == "max" else int(v)
    except (OSError, ValueError):
        return None


def unit_cgroup(m):
    return CG / PARENT / m["slice"] / (m["unit"] + ".service")


def job_mem(m):
    """Bytes in use now by a running job."""
    if SYS == "Linux" and m.get("unit"):
        return cg_int(unit_cgroup(m) / "memory.current") or 0
    if SYS == "Darwin" and m.get("child_pgid"):
        return mac_group_rss(m["child_pgid"])
    return int(m.get("mem_now") or 0)


def mac_group_rss(pgid):
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-g", str(pgid)], capture_output=True, text=True, timeout=10).stdout
        return sum(int(x) for x in out.split()) * 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def available():
    """Bytes the OS can give to a new process without swapping much."""
    if SYS == "Linux":
        for ln in open("/proc/meminfo"):
            if ln.startswith("MemAvailable:"):
                return int(ln.split()[1]) * 1024
    if SYS == "Darwin":
        out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
        ps = int(re.search(r"page size of (\d+)", out).group(1))
        pages = {k.strip(): int(v.strip().rstrip(".")) for k, v in re.findall(r"^Pages ([^:]+):\s+(\d+\.?)$", out, re.M)}
        return (pages.get("free", 0) + pages.get("inactive", 0) + pages.get("speculative", 0) + pages.get("purgeable", 0)) * ps
    if SYS == "Windows":
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + \
                       [(n, ctypes.c_ulonglong) for n in ("tp", "ap", "tpf", "apf", "tv", "av", "aev")]
        s = MS()
        s.dwLength = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
        return s.ap
    return 0


def reservation_files():
    try:
        dirs = json.loads(CONF.read_text()).get("reservation_dirs", [])
    except (OSError, ValueError, AttributeError):
        dirs = []
    return [p for d in dirs if Path(d).is_dir() for p in Path(d).glob("*.json")]


def others_unused():
    """Memory that other job tools have reserved for running jobs but not used yet (Linux only)."""
    tot = 0
    for p in reservation_files():
        try:
            e = json.loads(p.read_text())
            if e.get("state") != "run" or not Path("/proc/%s" % e.get("pid")).exists():
                continue
            used = sum(cg_int(CG / s.strip("/") / "memory.current") or 0 for s in e.get("scopes", []))
            tot += max(0, int(float(e.get("peak_gb", 0)) * GB) - used)
        except (OSError, ValueError, TypeError):
            pass
    return tot


def room(jobs):
    """Bytes a new job may reserve now."""
    r = available() - MARGIN_GB * GB
    if SYS == "Linux":
        lim = cg_int(CG / PARENT / "memory.high") or cg_int(CG / PARENT / "memory.max")
        if lim:
            r = min(r, lim - (cg_int(CG / PARENT / "memory.current") or 0))
        r -= others_unused()
    for m in jobs:
        if m.get("state") == "run":
            r -= max(0, int(m["peak_gb"] * GB) - job_mem(m))
    return r


# ---------------------------------------------------------------- process checks
def alive(pid):
    if not pid:
        return False
    if SYS == "Windows":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def all_jobs():
    out = []
    if STATE.is_dir():
        for d in STATE.iterdir():
            m = read(d.name) if d.is_dir() else None
            if m:
                if m.get("state") not in ENDED and m.get("sup_pid") and not alive(m["sup_pid"]):
                    if SYS == "Linux" and m.get("unit") and unit_active(m["unit"]):
                        pass
                    elif now() - m.get("created", 0) > 30:
                        m = update(m["id"], state="lost", ended=m.get("ended") or now())
                out.append(m)
    return sorted(out, key=lambda m: m.get("created", 0))


def unit_active(unit):
    r = subprocess.run(["systemctl", "is-active", "--quiet", unit + ".service"])
    return r.returncode == 0


# ---------------------------------------------------------------- run
def guess_project(cwd):
    try:
        top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5).stdout.strip()
        if top:
            return os.path.basename(top)
    except (OSError, subprocess.SubprocessError):
        pass
    return "misc"


def cmd_run(a):
    if not a.cmd:
        sys.exit("job run: give a command after --")
    cmd = a.cmd[1:] if a.cmd[0] == "--" else a.cmd
    cwd = os.path.abspath(a.cwd or os.getcwd())
    STATE.mkdir(parents=True, exist_ok=True)
    name = slug(a.name or os.path.basename(cmd[0]) + "-" + "-".join(slug(x, 12) for x in cmd[1:3]), 30)
    jid = time.strftime("%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    project = slug(a.project or guess_project(cwd), 30).lower()
    mem_gb = a.mem_gb or max(2.0, round(a.peak_gb * 1.5, 1))
    if a.peak_gb > mem_gb:
        sys.exit("job run: --peak-gb is more than --mem-gb")
    owner = a.owner or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID") or ""
    d = STATE / jid
    d.mkdir()
    m = {"id": jid, "name": name, "project": project, "owner": owner, "host": platform.node(), "os": SYS, "cmd": cmd,
         "cwd": cwd, "peak_gb": a.peak_gb, "mem_gb": mem_gb, "swap_gb": a.swap_gb, "state": "queued", "created": now(),
         "passes": 0, "user": os.environ.get("USER") or os.environ.get("USERNAME") or "", "version": 1}
    log = d / "job.log"
    log.touch()
    if SYS == "Linux":
        m["unit"] = "agentjob-" + jid
        m["slice"] = "agentjobs-%s.slice" % project.replace("-", "_").replace(".", "_")
        write(m)
        env = d / "env"
        env.write_text("".join("%s=%s\n" % (k, v) for k, v in os.environ.items()
                               if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k) and "\n" not in v and "\\" not in v and '"' not in v))
        os.chmod(str(env), 0o600)
        pre = [] if os.geteuid() == 0 else ["sudo", "-n"]
        sr = pre + ["systemd-run", "--quiet", "--collect", "--unit", m["unit"], "--slice", m["slice"],
                    "--uid", str(os.getuid()), "--gid", str(os.getgid()),
                    "-p", "WorkingDirectory=" + cwd, "-p", "EnvironmentFile=" + str(env),
                    "-p", "MemoryMax=%dM" % int(mem_gb * 1024), "-p", "MemorySwapMax=%dM" % int(a.swap_gb * 1024),
                    "-p", "OOMScoreAdjust=500", "-p", "Nice=10", "-p", "CPUWeight=20", "-p", "IOWeight=20",
                    "-p", "StandardOutput=append:" + str(log), "-p", "StandardError=append:" + str(log),
                    sys.executable, SELF, "_supervise", jid]
        r = subprocess.run(sr, capture_output=True, text=True)
        if r.returncode:
            update(jid, state="failed", error=(r.stderr or r.stdout).strip()[-500:], ended=now())
            sys.exit("job run: systemd-run failed (passwordless sudo needed): " + (r.stderr or r.stdout).strip())
    elif SYS == "Darwin":
        write(m)
        p = subprocess.Popen([sys.executable, SELF, "_supervise", jid], cwd=cwd, stdin=subprocess.DEVNULL,
                             stdout=open(log, "ab"), stderr=subprocess.STDOUT, start_new_session=True)
        update(jid, sup_pid=p.pid)
    elif SYS == "Windows":
        write(m)
        # Win32_Process.Create starts the supervisor outside the SSH session's job, so it outlives the connection.
        line = subprocess.list2cmdline([sys.executable, SELF, "_supervise", jid])
        ps = "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=%s; CurrentDirectory=%s}; $r.ProcessId" % (
            "'" + line.replace("'", "''") + "'", "'" + cwd.replace("'", "''") + "'")
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True)
        pid = int(re.findall(r"\d+", r.stdout or "0")[-1] or 0)
        update(jid, sup_pid=pid)
    print(jid)
    print("job %s: %s, peak %g GB, cap %g GB. Watch: job ls / job logs %s / job wait %s" % (jid, project, a.peak_gb, mem_gb, jid, jid), file=sys.stderr)


# ---------------------------------------------------------------- supervisor (runs inside the job)
def admit(jid):
    """Wait for memory. First come first served; a younger job may pass an older waiter that does not fit,
    at most MAX_PASSES times, then the younger jobs wait too."""
    last = 0
    while True:
        with Lock():
            jobs = all_jobs()
            me = next(m for m in jobs if m["id"] == jid)
            if me.get("state") == "stopped":
                return False
            older = [m for m in jobs if m.get("state") == "queued" and m.get("created", 0) < me["created"] and m["id"] != jid]
            r = room(jobs)
            blocked = any(m.get("passes", 0) >= MAX_PASSES for m in older)
            if r >= me["peak_gb"] * GB and not blocked:
                for m in older:
                    update(m["id"], passes=m.get("passes", 0) + 1)
                update(jid, state="run", started=now())
                return True
        if now() - last > 600:
            print("[job] waiting for memory: need %.1f GB, room %.1f GB%s" % (me["peak_gb"], r / GB, ", an older job waits first" if blocked else ""), flush=True)
            last = now()
        time.sleep(POLL)


def supervise(jid):
    if SYS == "Windows":  # started by WMI with no output handles: write to the job log
        f = open(str(STATE / jid / "job.log"), "a", buffering=1)
        sys.stdout = sys.stderr = f
    m = update(jid, sup_pid=os.getpid())
    if not admit(jid):
        return
    print("[job] start %s: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(shlex.quote(x) for x in m["cmd"])), flush=True)
    if SYS == "Windows":
        rc, sig, peak, why = win_run(m)
    else:
        rc, sig, peak, why = posix_run(m)
    if SYS == "Linux":
        cgd = Path("/proc/self/cgroup").read_text().strip().split("::", 1)[-1]
        peak = cg_int(CG / cgd.strip("/") / "memory.peak") or peak
        ev = (CG / cgd.strip("/") / "memory.events")
        try:
            evs = dict(l.split() for l in ev.read_text().splitlines())
            if int(evs.get("oom_kill", 0)):
                why = why or "over its memory cap (%g GB): the kernel killed it" % m["mem_gb"]
        except (OSError, ValueError):
            pass
    st = "done" if rc == 0 else ("killed" if why or sig in (9,) else "failed")
    if (read(jid) or {}).get("state") == "stopped":
        st = "stopped"
    update(jid, state=st, exit=rc, signal=sig, peak_bytes=peak, why=why, ended=now())
    print("[job] end %s: %s exit=%s signal=%s peak=%.2f GB %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), st, rc, sig, (peak or 0) / GB, why or ""), flush=True)


def posix_run(m):
    p = subprocess.Popen(m["cmd"], cwd=m["cwd"], start_new_session=True,
                         preexec_fn=(lambda: os.nice(10)) if SYS == "Darwin" else None)
    update(m["id"], child_pid=p.pid, child_pgid=p.pid)
    m["child_pgid"] = p.pid

    def fwd(signum, frame):
        try:
            os.killpg(p.pid, signum)
        except OSError:
            pass
    signal.signal(signal.SIGTERM, fwd)
    signal.signal(signal.SIGINT, fwd)
    peak, why, t_last = 0, None, 0
    while p.poll() is None:
        if SYS == "Darwin":
            rss = mac_group_rss(p.pid)
            peak = max(peak, rss)
            if now() - t_last > 15:
                update(m["id"], mem_now=rss, peak_bytes=peak)
                t_last = now()
            if rss > m["mem_gb"] * GB and not why:
                why = "over its memory cap (%g GB, RSS %.1f GB): stopped by job" % (m["mem_gb"], rss / GB)
                print("[job] " + why, flush=True)
                os.killpg(p.pid, signal.SIGTERM)
                for _ in range(20):
                    if p.poll() is not None:
                        break
                    time.sleep(0.5)
                if p.poll() is None:
                    os.killpg(p.pid, signal.SIGKILL)
        time.sleep(1)
    rc = p.returncode
    return (rc if rc >= 0 else None), (-rc if rc < 0 else None), peak, why


def win_run(m):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32

    class IOC(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("a", "b", "c", "d", "e", "f")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32), ("SchedulingClass", ctypes.c_uint32)]

    class EXT(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("Io", IOC), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    hjob = k32.CreateJobObjectW(None, None)
    info = EXT()
    info.Basic.LimitFlags = 0x200 | 0x2000  # JOB_MEMORY, KILL_ON_JOB_CLOSE
    info.JobMemoryLimit = int(m["mem_gb"] * GB)
    k32.SetInformationJobObject(wintypes.HANDLE(hjob), 9, ctypes.byref(info), ctypes.sizeof(info))
    log = open(str(STATE / m["id"] / "job.log"), "ab")
    p = subprocess.Popen(m["cmd"], cwd=m["cwd"], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         creationflags=0x4 | 0x4000)  # CREATE_SUSPENDED, BELOW_NORMAL_PRIORITY_CLASS
    k32.AssignProcessToJobObject(wintypes.HANDLE(hjob), wintypes.HANDLE(int(p._handle)))
    ctypes.windll.ntdll.NtResumeProcess(wintypes.HANDLE(int(p._handle)))
    update(m["id"], child_pid=p.pid)
    t_last = 0
    while p.poll() is None:
        if now() - t_last > 15:
            k32.QueryInformationJobObject(wintypes.HANDLE(hjob), 9, ctypes.byref(info), ctypes.sizeof(info), None)
            update(m["id"], peak_bytes=info.PeakJobMemoryUsed)
            t_last = now()
        time.sleep(1)
    k32.QueryInformationJobObject(wintypes.HANDLE(hjob), 9, ctypes.byref(info), ctypes.sizeof(info), None)
    peak = min(info.PeakJobMemoryUsed, info.JobMemoryLimit)  # a failed allocation also counts in the peak
    why = None
    if peak >= info.JobMemoryLimit * 0.98 and p.returncode != 0:
        why = "reached its memory cap (%g GB): allocations failed" % m["mem_gb"]
    return p.returncode, None, peak, why


# ---------------------------------------------------------------- ls / logs / wait / stop / install
def find(jid):
    if (STATE / jid).is_dir():
        return jid
    hits = [d.name for d in STATE.iterdir() if d.name.endswith(jid) or d.name.startswith(jid)] if STATE.is_dir() else []
    if len(hits) != 1:
        sys.exit("job: no single job matches %r" % jid)
    return hits[0]


def cmd_ls(a):
    jobs = all_jobs()
    if not a.all:
        cut = now() - 86400
        jobs = [m for m in jobs if m.get("state") not in ENDED or (m.get("ended") or 0) > cut]
    for m in jobs:
        if m.get("state") == "run":
            m["mem_now"] = job_mem(m)
    if a.json:
        print(json.dumps({"host": platform.node(), "os": SYS, "room": room(all_jobs()), "jobs": jobs}))
        return
    print("%-24s %-8s %-14s %-22s %7s %7s %5s %7s  %s" % ("ID", "STATE", "PROJECT", "NAME", "MEM", "PEAK", "CAP", "AGE", "OWNER"))
    for m in jobs:
        age = (m.get("ended") or now()) - (m.get("started") or m.get("created"))
        print("%-24s %-8s %-14s %-22s %6.1fG %6.1fG %4gG %6.0fm  %s" % (
            m["id"], m.get("state"), m.get("project", "")[:14], m.get("name", "")[:22], (m.get("mem_now") or 0) / GB,
            (m.get("peak_bytes") or 0) / GB, m.get("mem_gb"), age / 60, (m.get("owner") or "")[:36]))
    print("room for a new job now: %.1f GB" % (room(all_jobs()) / GB))


def cmd_logs(a):
    p = STATE / find(a.id) / "job.log"
    if a.follow:
        os.execvp("tail", ["tail", "-n", str(a.n), "-f", str(p)])
    lines = p.read_text(errors="replace").splitlines()
    print("\n".join(lines[-a.n:]))


def cmd_wait(a):
    jid = find(a.id)
    t0 = now()
    while True:
        m = next((x for x in all_jobs() if x["id"] == jid), None)
        if not m or m.get("state") == "lost":
            sys.exit(2)
        if m.get("state") in ENDED:
            print(json.dumps({k: m.get(k) for k in ("id", "state", "exit", "signal", "why", "peak_bytes")}))
            sys.exit(0)
        if a.timeout is not None and now() - t0 >= a.timeout:
            sys.exit(1)
        time.sleep(5)


def cmd_stop(a):
    jid = find(a.id)
    m = read(jid)
    if m.get("state") in ENDED:
        print("already " + m["state"])
        return
    update(jid, state="stopped", ended=now())
    if SYS == "Linux":
        subprocess.run(([] if os.geteuid() == 0 else ["sudo", "-n"]) + ["systemctl", "stop", m["unit"] + ".service"])
    elif SYS == "Darwin":
        for pid in (m.get("child_pgid"),):
            try:
                os.killpg(pid, signal.SIGTERM)
            except (OSError, TypeError):
                pass
        try:
            os.kill(m["sup_pid"], signal.SIGTERM)
        except (OSError, KeyError, TypeError):
            pass
    elif SYS == "Windows":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(m.get("sup_pid"))], capture_output=True)
    print("stopped " + jid)


def cmd_install(a):
    if SYS != "Linux":
        sys.exit("job install is for Linux; on macOS and Windows copy this file to a folder in PATH")
    if os.geteuid() != 0:
        sys.exit("run with sudo")
    tot = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / GB
    high = a.high_gb or round(tot * 0.35)
    mx = a.max_gb or round(high * 1.15)
    Path("/etc/systemd/system/agentjobs.slice").write_text(
        "# All agent batch jobs started with 'job run' (agentjobs-<project>.slice below this one).\n"
        "# The cap is for all of them together, so they cannot take the memory of services.\n"
        "[Unit]\nDescription=Agent batch jobs (job run)\nBefore=slices.target\n\n"
        "[Slice]\nCPUWeight=20\nIOWeight=20\nMemoryHigh=%dG\nMemoryMax=%dG\n" % (high, mx))
    dst = "/usr/local/bin/job"
    if os.path.realpath(dst) != SELF:
        data = Path(SELF).read_bytes()
        Path(dst).write_bytes(data)
        os.chmod(dst, 0o755)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print("agentjobs.slice: MemoryHigh=%dG MemoryMax=%dG of %.0f GB RAM; %s installed" % (high, mx, tot, dst))


def main():
    ap = argparse.ArgumentParser(prog="job", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="sub", required=True)
    r = sp.add_parser("run")
    r.add_argument("--project")
    r.add_argument("--name")
    r.add_argument("--owner")
    r.add_argument("--cwd")
    r.add_argument("--peak-gb", type=float, default=2.0, help="memory the job needs at its peak (admission)")
    r.add_argument("--mem-gb", type=float, help="hard cap; default 1.5x peak, at least 2")
    r.add_argument("--swap-gb", type=float, default=1.0, help="swap the job may use (Linux)")
    r.add_argument("cmd", nargs=argparse.REMAINDER)
    l = sp.add_parser("ls")
    l.add_argument("--json", action="store_true")
    l.add_argument("--all", action="store_true")
    g = sp.add_parser("logs")
    g.add_argument("id")
    g.add_argument("-n", type=int, default=50)
    g.add_argument("-f", "--follow", action="store_true")
    w = sp.add_parser("wait")
    w.add_argument("id")
    w.add_argument("--timeout", type=float)
    s = sp.add_parser("stop")
    s.add_argument("id")
    i = sp.add_parser("install")
    i.add_argument("--high-gb", type=float)
    i.add_argument("--max-gb", type=float)
    sv = sp.add_parser("_supervise")
    sv.add_argument("id")
    a = ap.parse_args()
    {"run": cmd_run, "ls": cmd_ls, "logs": cmd_logs, "wait": cmd_wait, "stop": cmd_stop, "install": cmd_install,
     "_supervise": lambda a: supervise(a.id)}[a.sub](a)


if __name__ == "__main__":
    main()

