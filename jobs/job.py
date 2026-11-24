#!/usr/bin/env python3
"""job: run a heavy command as a tracked job with memory caps and memory/CPU admission.

  job run [--project P] [--name N] [--peak-gb G] [--mem-gb M] [--owner ID] -- COMMAND ARG...
  job ls [--json] [--all]        jobs of this user on this machine
  job logs ID [-n 50] [-f]
  job wait ID [--timeout S]      exit 0 ended, 1 still running at timeout, 2 missing or lost
  job stop ID
  sudo job install [--max-gb M]  Linux: create agentjobs.slice, install /usr/local/bin/job

Why: agents start batch jobs with nohup or '&' in an SSH shell. The job then has no owner, no memory limit, and no
queue, and several of them can push a machine into swap thrashing. Each job here:
  - outlives the shell (Linux: a systemd unit; macOS/Windows: a detached supervisor),
  - waits until the machine has --peak-gb of free memory (admission; first come first served, with backfill),
  - is stopped at --mem-gb (Linux: cgroup MemoryMax; Windows: Job Object; macOS: RSS watchdog),
  - runs at low CPU priority, and is killed before services when memory runs out (oom_score_adj 500),
  - records owner (CODEX_THREAD_ID or --owner), project, command, exit code and peak memory in ~/.agentjobs/ID/.
Linux jobs go to agentjobs-<project>.slice under agentjobs.slice; 'job install' caps that parent slice, so all
agent jobs together cannot take the memory of services. Only jobs of this tool reserve memory: there is no
way for another tool to hold room in the queue, because a stale hold from a broken tool blocks every job.
"""
import argparse, json, os, platform, re, shlex, signal, subprocess, sys, time, uuid
from pathlib import Path

SYS = platform.system()
GB = 2 ** 30
STATE = Path(os.environ.get("AGENTJOBS_DIR") or (Path.home() / ".agentjobs"))
SELF = os.path.realpath(__file__)
MARGIN_ENV = os.environ.get("AGENTJOBS_MARGIN_GB")
CG = Path("/sys/fs/cgroup")
PARENT = "agentjobs.slice"
LOCK = "/dev/shm/agentjobs.lock" if SYS == "Linux" else str(STATE / ".lock")
STARVE_S = 3600  # after this wait, a queued job holds back the room it needs from younger jobs
SMALL_GB = 1.0  # a job this small is not held back for an older waiter
POLL = 3
LOCK_STALE_S = 60  # the admission lock is held for milliseconds; after this long its holder is paused
GRACE_S = 120  # a new job keeps its full stated peak for this long, then its reservation follows what it uses
PSI_FULL_MAX = 5.0  # no new job starts while memory pressure (full avg10, percent) is above this
HISTORY_X = 1.1  # a known kind of job reserves this times its largest recent peak (1.25 left 26% of that room unused)
REGROW_X = 0.25  # share of the possible regrowth that is held for running jobs other than the one with the most
# Linux: no prediction. A job starts when the host has real free memory. A watcher pauses a job when memory is short.
START_GB = 2.0  # free memory (after the margin) that a new job needs, or half of its --peak-gb hint if that is more
YOUNG_S = 60  # a job this young has not grown yet: half of its hint still counts as used
LOW_FRAC = 0.05  # the watcher acts when MemAvailable is below this share of RAM (at least 2 GB) ...
PSI_ACT = 5.0  # ... or when memory pressure (full avg10, percent) is above this, for HOT_S
PSI_ROOM = 3  # ... but not while MemAvailable is above this many times the low mark (15% of RAM)
HOT_S = 6
ACT_GAP_S = 15  # time between two watcher actions, to see the effect of the first
CALM_S = 30  # memory must be calm this long before a paused job resumes
STUCK_S = 1800  # a job paused this long resumes without the margin; if its swap cannot fit in free memory it is killed (alone: now; else after 4x)
IMMUNE_S = 300  # a resumed job is not paused again for this long, if another job can be
READBACK_S = 600  # a resumed job reads the memory that it needs back from swap in this time; what stays in swap after is cold
SMALL_VICTIM = 0.5  # GB; a job below this gives no relief and is never paused
SWAP_KEEP = 2.0  # GB of swap that a pause never uses
MAX_PAUSES = 12  # a job due for this pause is killed: the host cannot hold it, and its pauses keep the door shut
SWAP_DOOR = 0.1  # no new job starts when less than this share of the swap is free (at least 4 GB): pauses need swap
FULL_EXIT = 75  # exit code of 'job run' when the host is full
CPU_X = float(os.environ.get("AGENTJOBS_CPU_X") or 2.0)  # a job is sent to another host when the load is above this times the cores ...
PEER_FREE = 2  # ... and another host has at least this many free cores, and memory room
PEER_STALE_S = 180  # the peers file (written by 'job _peers' from another machine) is ignored when older than this
ENDED = ("done", "failed", "killed", "stopped", "lost")
THREAD_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
               "VECLIB_MAXIMUM_THREADS", "NUMBA_NUM_THREADS", "POLARS_MAX_THREADS", "RAYON_NUM_THREADS")


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


def lock_gen():
    """The newest lock file: LOCK itself (0) or LOCK.N."""
    d, base = os.path.split(LOCK)
    try:
        return max([0] + [int(n[len(base) + 1:]) for n in os.listdir(d) if n.startswith(base + ".") and n[len(base) + 1:].isdigit()])
    except OSError:
        return 0


def posix_lock():
    """Take the admission lock. The lock is held for milliseconds. If its holder does not release it for
    LOCK_STALE_S (a supervisor that a cgroup freeze or SIGSTOP paused while it held the lock), all waiters move
    to a new lock file. Without this, one paused job stops the queue of every project on the host."""
    import fcntl
    t0 = time.time()
    while True:
        gen = lock_gen()
        path = LOCK if gen == 0 else "%s.%d" % (LOCK, gen)
        f = open(path, "a+")
        try:
            os.chmod(path, 0o666)
        except OSError:
            pass
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if lock_gen() == gen:
                    return f
                break  # a newer lock file exists: use it
            except OSError:
                if lock_gen() != gen:
                    break
                if time.time() - t0 > LOCK_STALE_S:
                    print("[job] the admission lock was held for %d s by a paused process; the queue moves to a new lock"
                          % LOCK_STALE_S, file=sys.stderr, flush=True)
                    open("%s.%d" % (LOCK, gen + 1), "a+").close()
                    t0 = time.time()
                    break
                time.sleep(0.05)
        f.close()


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
            self.f.close()
            self.f = posix_lock()
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


def cg_anon(group):
    """Bytes of anon + shmem memory in a cgroup: what the job really holds."""
    try:
        st = dict(l.split() for l in (Path(group) / "memory.stat").read_text().splitlines())
        return int(st.get("anon", 0)) + int(st.get("shmem", 0))
    except (OSError, ValueError):
        return 0


def margin():
    """Bytes to keep free for agents and services: 8% of RAM, at least 3 GB."""
    if MARGIN_ENV:
        return float(MARGIN_ENV) * GB
    return max(3 * GB, 0.08 * total_ram()) if SYS == "Linux" else 4 * GB


def mem_pressure():
    """Linux memory PSI 'full avg10' in percent (0 elsewhere)."""
    try:
        for ln in open("/proc/pressure/memory"):
            if ln.startswith("full"):
                return float(ln.split("avg10=")[1].split()[0])
    except (OSError, IndexError, ValueError):
        pass
    return 0.0


def reserved(m):
    """Bytes a running job may still grow by. For GRACE_S after start: its stated peak.
    Then: 1.5x the peak it has really used (at least 0.2 GB), never more than the stated peak."""
    stated = int(m["peak_gb"] * GB)
    if now() - (m.get("started") or now()) > GRACE_S:
        seen = max(int(m.get("anon_peak") or 0), job_mem(m))
        stated = min(stated, max(int(0.2 * GB), int(seen * 1.5)))
    return max(0, stated - job_mem(m))


def name_key(name):
    """Jobs whose names differ only in digits (dates, shard numbers) are the same kind of job."""
    return re.sub(r"\d+", "#", name or "")


# ---------------------------------------------------------------- Linux: start on real free memory, pause on real pressure
def priv(argv, **kw):
    return subprocess.run(([] if os.geteuid() == 0 else ["sudo", "-n"]) + argv, capture_output=True, text=True, **kw)


def cg_write(path, value):
    """Write to a cgroup file as root. True on success."""
    try:
        if os.geteuid() == 0:
            Path(path).write_text(str(value))
            return True
    except OSError:
        return False
    try:  # one write() call; 'tee' would try again without end when memory.reclaim returns EAGAIN
        return subprocess.run(["sudo", "-n", "sh", "-c", 'echo "$1" > "$2"', "sh", str(value), str(path)],
                              capture_output=True, text=True, timeout=120).returncode == 0
    except subprocess.SubprocessError:
        return False


def swap_total():
    try:
        for ln in open("/proc/meminfo"):
            if ln.startswith("SwapTotal:"):
                return int(ln.split()[1]) * 1024
    except OSError:
        pass
    return 0


def swap_free():
    try:
        for ln in open("/proc/meminfo"):
            if ln.startswith("SwapFree:"):
                return int(ln.split()[1]) * 1024
    except OSError:
        pass
    return 0


def is_paused(m):
    """True when the watcher paused this running job. The cgroup is the truth: a supervisor that was frozen in the
    middle of a record update can write an old record back."""
    if m.get("state") != "run" or not m.get("unit"):
        return False
    try:
        return (unit_cgroup(m) / "cgroup.freeze").read_text().strip() == "1"
    except OSError:
        return False


def free_now(jobs, skip=None):
    """Bytes free for a new job now: MemAvailable minus the margin, inside the slice cap, minus half of the hint of
    each job that started in the last YOUNG_S (it has not grown yet)."""
    r = available() - margin()
    lim = cg_int(CG / PARENT / "memory.max")
    if lim:
        r = min(r, lim - cg_anon(CG / PARENT))
    for m in jobs:
        if m.get("state") == "run" and m.get("id") != skip and now() - (m.get("started") or 0) < YOUNG_S:
            r -= max(0, need_gb(m) * GB - job_mem(m))
        if m.get("state") == "run" and m.get("id") != skip and m.get("unit") and not is_paused(m) \
                and now() - (m.get("resumed") or 0) < READBACK_S:
            # a resumed job reads its swap back into memory (alphas-1 Nov 23: 4 queued jobs started beside a job
            # with 7 GB in swap, and 3 were killed a minute later). Only for READBACK_S: swap that a job did not
            # read back by then is memory that it does not use (tingbox2 Nov 23: 10 GB available, 8 GB of old swap,
            # 7 jobs queued)
            r -= cg_int(unit_cgroup(m) / "memory.swap.current") or 0
    return r


def need_gb(m):
    """GB of free memory that a job needs at the door: the measured peak of earlier runs of this kind of job when
    there is one (the job is known), else half of the --peak-gb hint (hints are rough). At least START_GB."""
    return max(START_GB, float(m.get("peak_known") or 0) or float(m.get("peak_given") or 0) / 2)


def door(me, jobs):
    """(open, reason). The door for a new job on Linux. It looks only at measured values."""
    paused = [m for m in jobs if is_paused(m)]
    if paused:
        return False, "%d job%s paused here because memory is short" % (len(paused), " is" if len(paused) == 1 else "s are")
    low = max(2 * GB, LOW_FRAC * total_ram())
    if mem_pressure() > PSI_FULL_MAX and available() <= PSI_ROOM * low:  # same rule as the watcher
        return False, "memory pressure is high"
    st = swap_total()
    if st and swap_free() < max(4 * GB, SWAP_DOOR * st):
        # tingbox Nov 23: 105 jobs, 54 of 55 GB swap used, 7 jobs killed in 1 h because no job could be paused
        return False, "swap is almost full (%.1f GB free)" % (swap_free() / GB)
    need = need_gb(me) * GB
    free = free_now(jobs, me.get("id"))
    if free < need:
        return False, "%.1f GB is free, this job needs %.1f GB to start%s" % (
            max(0, free) / GB, need / GB, " (earlier runs of this kind of job used that much)" if me.get("peak_known") else "")
    return True, ""


def watch_log(m, text):
    try:
        with open(str(STATE / m["id"] / "job.log"), "a") as f:
            f.write("[job] %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except OSError:
        pass
    print("%s %s %s/%s: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), m["id"], m.get("project"), m.get("name"), text), flush=True)


def victims(jobs):
    """Jobs that can be paused, best choice first: the project that uses the most job memory first, and in a project
    the youngest job first. Jobs below SMALL_VICTIM and jobs with --no-pause are never chosen. A job that resumed
    less than IMMUNE_S ago comes after all others."""
    c = [m for m in jobs if m.get("state") == "run" and not is_paused(m) and not m.get("no_pause")]
    for m in c:
        m["_mem"] = job_mem(m)
    c = [m for m in c if m["_mem"] >= SMALL_VICTIM * GB]
    use = {}
    for m in c:
        use[m.get("project")] = use.get(m.get("project"), 0) + m["_mem"]
    return sorted(c, key=lambda m: (now() - (m.get("resumed") or 0) <= IMMUNE_S, -use[m.get("project")], -(m.get("started") or 0)))


def kill_job(m, why):
    update(m["id"], state="killed", why=why, watch_why=why, ended=now(), signal=9, peak_bytes=max(m.get("peak_bytes") or 0, m.get("_mem") or 0))
    watch_log(m, "killed: " + why)
    event("kill", m, why=why)
    cg_write(unit_cgroup(m) / "cgroup.freeze", 0)
    priv(["systemctl", "kill", "--signal=SIGKILL", m["unit"] + ".service"])


def event(ev, m=None, **kw):
    """Add one line to <state>/events.jsonl: each pause, resume, kill and rejected start, with the memory values at
    that time. 'job room' and the dashboard count these."""
    try:
        e = {"t": round(now()), "ev": ev, "avail_gb": round(available() / GB, 1), "psi": mem_pressure()}
        if m:
            e.update({k: m.get(k) for k in ("id", "project", "name", "owner")})
        e.update(kw)
        with open(STATE / "events.jsonl", "a") as f:
            f.write(json.dumps(e) + "\n")
    except (OSError, ValueError):
        pass


def event_counts(hours=24):
    """{event: count} for the last hours."""
    c, cut = {}, now() - hours * 3600
    try:
        with open(STATE / "events.jsonl") as f:
            for ln in f.readlines()[-5000:]:
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if e.get("t", 0) >= cut:
                    c[e.get("ev")] = c.get(e.get("ev"), 0) + 1
    except OSError:
        pass
    return c


def relieve(jobs, cause="", short=True):
    """Memory is short now. Pause the first job of victims() and move as much of its memory to swap as the swap can
    take (SWAP_KEEP stays free). A paused job stops growing and stops using its memory, so a part is enough.
    Kill only when a pause cannot help: swap is full, or the job is the only one here and it filled the host
    again after 2 pauses, or it was paused MAX_PAUSES times. A killed job is never started again."""
    order = victims(jobs)
    if not order:
        return None
    m = order[0]
    mem, cg = m["_mem"], unit_cgroup(m)
    space = swap_free() - SWAP_KEEP * GB
    if space < 1 * GB and short:
        kill_job(m, "memory was short and swap is full, so no job could be paused. This job (%.1f GB) was the newest "
                    "of the project that used the most memory. It was not started again. Use a host with room "
                    "('job where')." % (mem / GB))
        return "kill"
    others = [o for o in jobs if o.get("state") == "run" and o["id"] != m["id"]]
    if not others and (m.get("pauses") or 0) >= 2 and mem >= 0.25 * total_ram():
        kill_job(m, "it is the only job here and it filled the memory 3 times (%.1f GB now). It was not started "
                    "again. Use a host with more memory or make the job smaller." % (mem / GB))
        return "kill"
    if (m.get("pauses") or 0) >= MAX_PAUSES - 1:
        # alphas-1, Nov 24: two archive jobs of 3 and 6 GB took turns (25 and 7 pauses in 7 h); while one of them
        # was paused, no new job started, and 19 jobs waited up to 9 h. On 3 hosts, no job paused 4-9 times failed.
        kill_job(m, "memory was short again and it was paused %d times before (%.1f GB now): this host cannot "
                    "hold it beside the other jobs, and no new job starts here while a job is paused. It was not "
                    "started again. Use a host with room ('job where') or make the job smaller."
                 % (m.get("pauses") or 0, mem / GB))
        return "kill"
    with Lock():  # a supervisor that updates its record holds no lock while we freeze it
        if not cg_write(cg / "cgroup.freeze", 1):
            return None
        update(m["id"], paused=now(), pauses=(m.get("pauses") or 0) + 1)
    watch_log(m, "paused: host memory is short (%sthis job holds %.1f GB). It resumes automatically." % (cause and cause + "; ", mem / GB))
    event("pause", m, cause=cause, mem_gb=round(mem / GB, 1))
    cg_write(cg / "memory.swap.max", "max")
    if space >= 1 * GB and short:
        # Only pressure, memory is still available: the freeze alone must do. A move of the job to swap and back
        # was the main swap traffic on tingbox on Nov 24 (the same jobs paused each 5 minutes, 4 to 6 GB each time).
        cg_write(cg / "memory.reclaim", "%d" % min(mem, space))  # a partial result is fine
    return "pause"


def resume_one(jobs):
    """Resume the oldest paused job whose swapped memory fits in the free memory now, with the margin. The door
    stays shut while a job is paused, so a pause must end: when no other job runs or after STUCK_S, a job resumes
    as soon as its swap fits without the margin (the watcher pauses it again if it fills the host). A younger job
    that fits resumes before an older one that does not (alphas-1, Nov 24: a 6.3 GB and a 2.1 GB job paused 34 min,
    3.6 GB available; only the older one was tried, so neither resumed). When none fits, the oldest that waited too
    long is killed (alphas-1, Nov 23: 7.3 GB in swap, 9.6 GB available, frozen 8 h, 12 jobs queued)."""
    paused = sorted((m for m in jobs if is_paused(m)), key=lambda m: m.get("started") or 0)
    stuck = []
    for m in paused:
        cg = unit_cgroup(m)
        swapped = cg_int(cg / "memory.swap.current") or 0
        alone = not [o for o in jobs if o.get("state") == "run" and o["id"] != m["id"] and not is_paused(o)]
        waited = now() - (m.get("paused") or now())
        # The margin is not needed when no other job runs, and not after STUCK_S: the door is shut while a job is
        # paused, so a long pause blocks every new job (alphas-1, Nov 24: 5.3 GB in swap, 6.5 GB available, 9 small
        # long jobs beside it, paused 2 h, 16 jobs queued).
        spare = 0 if alone or waited > STUCK_S else margin() + START_GB * GB
        if available() < swapped + spare:
            if waited > (STUCK_S if alone else 4 * STUCK_S):
                stuck.append((m, swapped, alone))
            continue
        cg_write(cg / "cgroup.freeze", 0)
        cg_write(cg / "memory.swap.max", "%d" % int((m.get("swap_gb") or 1) * GB))
        t = m.get("paused") or now()
        update(m["id"], paused=None, resumed=now(), paused_s=(m.get("paused_s") or 0) + now() - t)
        watch_log(m, "resumed after %.0f s" % (now() - t))
        event("resume", m, paused_s=round(now() - t))
        return True
    if not stuck:
        return False
    m, swapped, alone = stuck[0]
    kill_job(m, "it was paused and could not resume: %.1f GB of it is in swap, only %.1f GB is free%s. It "
                "was not started again. Use a host with more memory ('job where') or make the job smaller."
             % (swapped / GB, available() / GB, " with no other job here" if alone else ""))
    return True


def cmd_watch(a):
    """Linux: the memory watcher (one per user, started by 'job install'). It reads real memory values every 2 s."""
    low = max(2 * GB, LOW_FRAC * total_ram())
    hot = calm = None
    acted = 0
    print("watcher: act below %.1f GB available or above %g%% memory pressure" % (low / GB, PSI_ACT), flush=True)
    while True:
        try:
            av, psi = available(), mem_pressure()
            lim = cg_int(CG / PARENT / "memory.max")
            near = bool(lim and cg_anon(CG / PARENT) > 0.9 * lim)
            # Pressure counts only when the host is also low on memory. With much memory available, the pressure
            # comes from one job at its own cap or from a resumed job that reads its memory back from swap. A pause
            # gives no relief there: it moves more memory to swap and makes more pressure.
            roomy = av > PSI_ROOM * low
            if av < low or (psi > PSI_ACT and not roomy) or near:
                hot, calm = hot or now(), None
                urgent = av < low / 2 or near  # near: the jobs are close to the slice cap, where the kernel kills
                if now() - hot >= (0 if urgent else HOT_S) and now() - acted >= (4 if urgent else ACT_GAP_S):
                    cause = "%.1f GB available" % (av / GB) if av < low else "jobs hold %.0f%% of their %.0f GB cap" % (
                        100.0 * cg_anon(CG / PARENT) / lim, lim / GB) if near else "pressure %.0f%%, %.1f GB available" % (psi, av / GB)
                    if relieve(all_jobs(), cause, short=av < low or near):
                        acted = now()
            else:
                hot = None
                if av - margin() > 0 and (psi < 1 or roomy):
                    calm = calm or now()
                    if now() - calm >= CALM_S and now() - acted >= ACT_GAP_S and resume_one(all_jobs()):
                        acted = now()
                else:
                    calm = None
        except Exception as e:  # the watcher must not stop
            print("watcher error: %r" % e, flush=True)
        time.sleep(2)


def history_peak(project, name, jobs=None):
    """(GB, runs): the peak to expect for this kind of job: HISTORY_X times the largest of its last 5 good runs.
    If the newest ended run was killed at its cap, the real peak is unknown: use 1.5x that cap.
    Returns (None, 0) when there is no history."""
    key, got = name_key(name), []
    for m in reversed(all_jobs() if jobs is None else jobs):
        if m.get("project") != project or name_key(m.get("name")) != key:
            continue
        ran = (m.get("ended") or 0) - (m.get("started") or m.get("ended") or 0)
        if m.get("state") == "done" and m.get("peak_bytes") and ran > 20:
            got.append(m["peak_bytes"] / GB)
        elif m.get("state") == "killed" and "cap" in (m.get("why") or ""):
            if not got:
                return round(float(m.get("mem_gb") or 0) * 1.5, 2), 1
            break  # older runs were before the kill: a later good run is the better guide
        if len(got) >= 5:
            break
    if not got:
        return None, 0
    return max(0.25, round(max(got) * HISTORY_X, 2)), len(got)


def plan(project, name, given, mem_given, jobs=None):
    """(peak_gb, mem_gb, note). History wins over the caller's guess. The cap is loose (3x the peak), because
    the queue and the slice cap protect the host; a tight cap only kills work when a guess is wrong."""
    learned, n = history_peak(project, name, jobs)
    peak = learned if learned else float(given or 2.0)
    if mem_given:
        mem = max(float(mem_given), round(peak * 1.5, 1)) if learned else float(mem_given)
    else:
        mem = max(2.0, round(peak * 3, 1), round(float(given or 0) * 1.5, 1))
    note = "peak %g GB from %d earlier run%s" % (peak, n, "" if n == 1 else "s") if learned else ""
    return peak, mem, note


def cpu_busy(jobs):
    """True when the CPUs are far over capacity and jobs already have a fair share: a new start would only
    slow all the others down."""
    if SYS != "Linux":
        return False
    n = os.cpu_count() or 1
    try:
        load1 = os.getloadavg()[0]
        some = float(open("/proc/pressure/cpu").readline().split("avg10=")[1].split()[0])
    except (OSError, IndexError, ValueError):
        return False
    running = sum(1 for m in jobs if m.get("state") == "run")
    return load1 > 2.5 * n and some > 40 and running >= max(2, n // 4)


def mac_cpu_slots():
    """Keep four logical CPUs for interactive tools and services."""
    return max(1, (os.cpu_count() or 1) - 4)


def mac_group_cpu(pgid):
    """Cores in use now by a process group (ps %cpu, a short average)."""
    try:
        out = subprocess.run(["ps", "-o", "%cpu=", "-g", str(pgid)], capture_output=True, text=True, timeout=10).stdout
        return sum(float(x) for x in out.split()) / 100
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def mac_cpu_reserved(m):
    """CPU slots a running Mac job holds. For GRACE_S after start: the CPUs it declared (old jobs: four).
    Then: 1.5x the cores it really uses (at least 0.1), never more than it declared. A job that waits on the
    network or sleeps as a service then holds almost no slot."""
    declared = m.get("cpus") or 4
    if m.get("child_pgid") and now() - (m.get("started") or now()) > GRACE_S:
        return min(declared, max(0.1, 1.5 * mac_group_cpu(m["child_pgid"])))
    return declared


def mac_cpu_blocked(me, jobs):
    """Reserve CPU slots under the admission lock, across all projects. Only CPU that jobs use can block a job.
    The load of the Mac is not a gate: apps, a VM and agent chats keep it above twice the core count for hours,
    and jobs of 0.2 GB then waited up to 6 hours for CPUs that no job used."""
    if SYS != "Darwin":
        return False
    used = sum(mac_cpu_reserved(m) for m in jobs if m.get("state") == "run")
    return used + (me.get("cpus") or 1) > mac_cpu_slots()


def thread_env(m):
    """The job's environment, with a thread count for math libraries that fits the free CPUs at start.
    Without it each numpy/BLAS/OpenMP process starts one thread per core, and N jobs make N x cores threads."""
    env = dict(os.environ)
    auto = env.get("JOB_THREADS_AUTO")  # a value equal to this came from a host default or an outer job, not from the caller
    n = os.cpu_count() or 1
    t = (m.get("cpus") or 1) if SYS == "Darwin" else m.get("cpus")
    if not t:
        try:
            load = os.getloadavg()[0]
        except (OSError, AttributeError):
            load = 0
        t = max(4, min(16, n, n - load))
    t = str(int(t))
    changed = False
    for k in THREAD_VARS:
        if SYS == "Darwin" or m.get("cpus") or k not in env or env[k] == auto:
            env[k] = t
            changed = True
    if changed:
        env["JOB_THREADS_AUTO"] = t
        print("[job] math threads per process: %s (set --cpus N or OMP_NUM_THREADS to choose)" % t, flush=True)
    return env


def unit_cgroup(m):
    return CG / PARENT / m["slice"] / (m["unit"] + ".service")


def job_mem(m):
    """Bytes in use now by a running job (on Linux: anon + shmem; file cache is free to reclaim)."""
    if SYS == "Linux" and m.get("unit"):
        return cg_anon(unit_cgroup(m))
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


def total_ram():
    """Physical RAM in bytes (0 if unknown)."""
    try:
        if SYS == "Linux":
            for ln in open("/proc/meminfo"):
                if ln.startswith("MemTotal:"):
                    return int(ln.split()[1]) * 1024
        if SYS == "Darwin":
            return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip())
        if SYS == "Windows":
            import ctypes
            kb = ctypes.c_ulonglong()
            ctypes.windll.kernel32.GetPhysicallyInstalledSystemMemory(ctypes.byref(kb))
            return kb.value * 1024
    except (OSError, ValueError, AttributeError):
        pass
    return 0


def room(jobs):
    """Bytes a new job may reserve now. A job in its first GRACE_S holds its full stated peak. An older job is
    below its own earlier peak most of the time, and jobs do not all go back to their peaks together: the room
    holds the largest such regrowth in full and REGROW_X of the others. The slice cap, the margin and the memory
    pressure check cover the rare case where more of them grow at the same time."""
    r = available() - margin()
    if SYS == "Linux":
        if mem_pressure() > PSI_FULL_MAX:
            return 0
        lim = cg_int(CG / PARENT / "memory.max")
        if lim:
            r = min(r, lim - cg_anon(CG / PARENT))
    regrow = []
    for m in jobs:
        if m.get("state") == "run":
            if now() - (m.get("started") or now()) > GRACE_S:
                regrow.append(reserved(m))
            else:
                r -= reserved(m)
    regrow.sort(reverse=True)
    return r - sum(regrow[:1]) - REGROW_X * sum(regrow[1:])


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
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", unit + ".service"], timeout=15).returncode == 0
    except subprocess.SubprocessError:
        return True  # systemd did not answer: do not call the job lost


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
    if a.cpus is not None and a.cpus < 1:
        sys.exit("job run: --cpus must be at least 1")
    if SYS == "Darwin":
        a.cpus = a.cpus or 1
        if a.cpus > mac_cpu_slots():
            sys.exit("job run: --cpus exceeds this Mac's %d CPU slots; use another host" % mac_cpu_slots())
    cwd = os.path.abspath(a.cwd or os.getcwd())
    STATE.mkdir(parents=True, exist_ok=True)
    name = slug(a.name or os.path.basename(cmd[0]) + "-" + "-".join(slug(x, 12) for x in cmd[1:3]), 30)
    jid = time.strftime("%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    project = slug(a.project or guess_project(cwd), 30).lower()
    given = a.peak_gb
    if SYS == "Linux":  # no prediction: the hint is used only at the door; the cap is the slice cap unless given
        a.peak_gb, mem_gb, note = float(given or START_GB), float(a.mem_gb or 0), ""
    else:
        a.peak_gb, mem_gb, note = plan(project, name, given, a.mem_gb)
    if note:
        print("job run: " + note + (" (you gave %g)" % given if given else ""), file=sys.stderr)
    if mem_gb and a.peak_gb > mem_gb:
        sys.exit("job run: --peak-gb is more than --mem-gb")
    lim = (total_ram() - margin()) if total_ram() else 0
    if SYS == "Linux":
        slim = cg_int(CG / PARENT / "memory.max")
        if not slim:
            try:  # the slice cgroup exists only while a job runs; ask systemd for the configured cap
                v = subprocess.run(["systemctl", "show", PARENT, "-p", "MemoryMax", "--value"],
                                   capture_output=True, text=True, timeout=5).stdout.strip()
                slim = int(v) if v.isdigit() else 0
            except (OSError, subprocess.SubprocessError, ValueError):
                slim = 0
        if slim:
            lim = min(lim, slim) if lim else slim
    if lim > 0:
        lim_gb = lim / GB
        if a.peak_gb > lim_gb * 0.9:
            sys.exit("job run: --peak-gb %g is too big for this host. Agent jobs here may use %.0f GB in total. "
                     "Split the job into smaller parts, or run it on a bigger host." % (a.peak_gb, lim_gb))
        mem_gb = min(mem_gb or lim_gb, round(lim_gb, 1))
    if SYS == "Linux":
        mem_gb = mem_gb or round(total_ram() / GB, 1)
        jobs = all_jobs()
        known = history_peak(project, name, jobs)[0]  # a known kind of job needs its measured peak, not a guess
        ok, why = door({"peak_given": given, "peak_known": known}, jobs)
        if not ok and not a.queue:
            print("job run: %s is full: %s. The job did NOT start. Use another host ('job where' shows the room on "
                  "each host), or add --queue to wait here." % (platform.node(), why), file=sys.stderr)
            event("reject", {"project": project, "name": name, "owner": a.owner or os.environ.get("CODEX_THREAD_ID") or ""}, why=why)
            sys.exit(FULL_EXIT)
        if not ok:
            print("job run: %s. This job waits (--queue)." % why, file=sys.stderr)
        if ok and not a.queue and not a.no_pause:
            b = better_host(given)
            if b:
                why = "no free CPU core here (load %.0f on %d cores); %s has %.0f free cores and %.0f GB of room" % (
                    b[2], b[3], b[0], b[1]["cores"] - b[1]["load"], b[1]["room_gb"])
                print("job run: %s is full: %s. The job did NOT start. Run it on %s, where it is faster. If it must "
                      "run on this host, add --queue: then it starts here now." % (platform.node(), why, b[0]), file=sys.stderr)
                event("reject", {"project": project, "name": name, "owner": a.owner or os.environ.get("CODEX_THREAD_ID") or ""},
                      why="cpu: " + why, to=b[0])
                sys.exit(FULL_EXIT)
    else:
        fits = room(all_jobs()) / GB
        if fits < a.peak_gb:
            print("job run: only %.1f GB is free for jobs now. This job waits until %g GB is free." % (max(0, fits), a.peak_gb), file=sys.stderr)
    owner = a.owner or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID") or ""
    d = STATE / jid
    d.mkdir()
    m = {"id": jid, "name": name, "project": project, "owner": owner, "host": platform.node(), "os": SYS, "cmd": cmd,
         "cwd": cwd, "peak_gb": a.peak_gb, "mem_gb": mem_gb, "swap_gb": a.swap_gb, "state": "queued", "created": now(),
         "peak_given": given, "cpus": a.cpus, "user": os.environ.get("USER") or os.environ.get("USERNAME") or "", "version": 1}
    if SYS == "Linux" and known:
        m["peak_known"] = known
    if a.no_pause:
        m["no_pause"] = True
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
        r = systemd_launch(m)
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
    if SYS == "Linux":
        try:
            load, n = os.getloadavg()[0], os.cpu_count() or 1
            if load > 1.25 * n:
                print("job run: note: %s has no free CPU core (load %.0f on %d cores), so this job runs slower here. "
                      "'job where' shows hosts with free cores." % (platform.node(), load, n), file=sys.stderr)
        except OSError:
            pass
    if a.wait:
        sys.exit(follow(jid))


def systemd_launch(m):
    """Start the supervisor of job m as a transient systemd service in its project slice."""
    d = STATE / m["id"]
    log = d / "job.log"
    pre = [] if os.geteuid() == 0 else ["sudo", "-n"]
    sr = pre + ["systemd-run", "--quiet", "--collect", "--unit", m["unit"], "--slice", m["slice"],
                "--uid", str(os.getuid()), "--gid", str(os.getgid()),
                "-p", "WorkingDirectory=" + m["cwd"], "-p", "EnvironmentFile=" + str(d / "env"),
                "-p", "MemoryMax=%dM" % int(m["mem_gb"] * 1024), "-p", "MemorySwapMax=%dM" % int(m["swap_gb"] * 1024),
                "-p", "OOMScoreAdjust=500",
                "-p", "StandardOutput=append:" + str(log), "-p", "StandardError=append:" + str(log),
                sys.executable, SELF, "_supervise", m["id"]]
    return subprocess.run(sr, capture_output=True, text=True)


def cmd_requeue(a):
    """Linux: restart the supervisors of queued jobs so that they run the current code. A queued job has not
    started its command, so this is safe; the job keeps its place (its 'created' time)."""
    pre = [] if os.geteuid() == 0 else ["sudo", "-n"]
    for m in all_jobs():
        if m.get("state") != "queued" or SYS != "Linux" or m.get("user") != (os.environ.get("USER") or ""):
            continue
        with Lock():  # hold the lock so the old supervisor cannot admit the job while we swap it
            if (read(m["id"]) or {}).get("state") != "queued":
                continue
            subprocess.run(pre + ["systemctl", "stop", m["unit"] + ".service"], capture_output=True)
            for _ in range(50):
                if not unit_active(m["unit"]):
                    break
                time.sleep(0.1)
            r = systemd_launch(m)
        print("%s %s" % (m["id"], "requeued" if r.returncode == 0 else "FAILED: " + (r.stderr or r.stdout).strip()))


def follow(jid):
    """Print the job's log as it grows; return the job's exit code when it ends (1 if it did not exit cleanly)."""
    log, pos = STATE / jid / "job.log", 0
    while True:
        m = read(jid) or {}
        try:
            with open(log, "rb") as f:
                f.seek(pos)
                chunk = f.read()
                pos += len(chunk)
            if chunk:
                sys.stdout.write(chunk.decode("utf-8", "replace"))
                sys.stdout.flush()
        except OSError:
            pass
        if m.get("state") in ENDED:
            return m.get("exit") if isinstance(m.get("exit"), int) else 1
        time.sleep(1)


# ---------------------------------------------------------------- supervisor (runs inside the job)
def self_mtime():
    try:
        return os.stat(SELF).st_mtime
    except OSError:
        return 0


def held_back(me, jobs, r):
    """Bytes of the room r that job 'me' must leave for an older waiter. Only the oldest waiter that has waited
    STARVE_S holds room back, and only if it can fit when all running jobs have ended (if not, the hold helps
    nobody). A job of SMALL_GB or less passes the hold: it delays the big job very little."""
    if me["peak_gb"] <= SMALL_GB:
        return 0
    empty = r + sum(reserved(m) + job_mem(m) for m in jobs if m.get("state") == "run")
    old = [m for m in jobs if m.get("state") == "queued" and m["id"] != me["id"] and m.get("created", 0) < me["created"]
           and now() - m.get("created", now()) > STARVE_S and m["peak_gb"] * GB <= empty]
    return min(old, key=lambda m: m.get("created", 0))["peak_gb"] * GB if old else 0


def admit(jid):
    """Wait for memory and CPU room. A job starts as soon as it fits; it may pass an older waiter that does not fit.
    When an older waiter has waited STARVE_S, its peak is held back from younger jobs (see held_back), so that it
    can start; younger jobs still start in the room that is left over."""
    last = 0
    code = self_mtime()
    while True:
        if SYS == "Darwin" and self_mtime() != code:  # the tool was updated: a waiting job loads the new rules
            print("[job] the job tool changed; this waiting job restarts its supervisor", flush=True)
            os.execv(sys.executable, [sys.executable, SELF, "_supervise", jid])
        with Lock():
            jobs = all_jobs()
            me = next(m for m in jobs if m["id"] == jid)
            if me.get("state") == "stopped":
                return False
            if SYS == "Linux":
                ok, why = door(me, jobs)
                if ok:
                    update(jid, state="run", started=now())
                    return True
        if SYS == "Linux":
            if now() - last > 600:
                print("[job] waiting: " + why, flush=True)
                last = now()
            time.sleep(POLL)
            continue
        with Lock():
            jobs = all_jobs()
            me = next(m for m in jobs if m["id"] == jid)
            if me.get("state") == "stopped":
                return False
            r = room(jobs)
            held = held_back(me, jobs, r)
            blocked = held > 0 and r >= me["peak_gb"] * GB > r - held
            busy = cpu_busy(jobs) or mac_cpu_blocked(me, jobs)
            if r - held >= me["peak_gb"] * GB and not busy:
                update(jid, state="run", started=now())
                return True
        if now() - last > 600:
            print("[job] waiting: need %.1f GB, room %.1f GB%s%s" % (me["peak_gb"], r / GB, ", an older job waits first" if blocked else "",
                                                                     ", the CPUs are over capacity" if busy else ""), flush=True)
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
    try:
        rc, sig, peak, why = win_run(m) if SYS == "Windows" else posix_run(m)
    except OSError as e:  # the command cannot start (missing program or cwd): end as failed, not as a job that runs
        print("[job] cannot start: %s" % e, flush=True)
        update(jid, error=str(e)[-500:])
        rc, sig, peak, why = 127, None, 0, None
    if SYS == "Linux":
        cgd = Path("/proc/self/cgroup").read_text().strip().split("::", 1)[-1]
        # memory.peak counts file cache too, so it is too high for --peak-gb; keep it apart, report the anon peak
        cg_peak = cg_int(CG / cgd.strip("/") / "memory.peak")
        if cg_peak:
            update(jid, cg_peak=cg_peak)
        ev = (CG / cgd.strip("/") / "memory.events")
        try:
            evs = dict(l.split() for l in ev.read_text().splitlines())
            if int(evs.get("oom_kill", 0)):
                why = why or "over its memory cap (%g GB): the kernel killed it" % m["mem_gb"]
        except (OSError, ValueError):
            pass
        why = (read(jid) or {}).get("watch_why") or why
    st = "done" if rc == 0 else ("killed" if why or sig in (9,) else "failed")
    if (read(jid) or {}).get("state") == "stopped":
        st = "stopped"
    update(jid, state=st, exit=rc, signal=sig, peak_bytes=peak, why=why, ended=now())
    print("[job] end %s: %s exit=%s signal=%s peak=%.2f GB %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), st, rc, sig, (peak or 0) / GB, why or ""), flush=True)


def posix_run(m):
    p = subprocess.Popen(m["cmd"], cwd=m["cwd"], start_new_session=True, env=thread_env(m),
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
    mycg = None
    if SYS == "Linux":
        mycg = CG / Path("/proc/self/cgroup").read_text().strip().split("::", 1)[-1].strip("/")
    while p.poll() is None:
        if mycg is not None:
            a = cg_anon(mycg)
            if a > peak:
                peak = a
            if now() - t_last > 10:
                update(m["id"], anon_peak=peak, peak_bytes=peak)
                t_last = now()
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
        print(json.dumps({"host": platform.node(), "os": SYS, "room": host_room(all_jobs()), "jobs": jobs}))
        return
    print("%-24s %-8s %-14s %-22s %7s %7s %5s %7s  %s" % ("ID", "STATE", "PROJECT", "NAME", "MEM", "PEAK", "CAP", "AGE", "OWNER"))
    for m in jobs:
        age = (m.get("ended") or now()) - (m.get("started") or m.get("created") or now())
        print("%-24s %-8s %-14s %-22s %6.1fG %6.1fG %4gG %6.0fm  %s" % (
            m["id"], "paused" if is_paused(m) else m.get("state"),
            (m.get("project") or "")[:14], (m.get("name") or "")[:22], (m.get("mem_now") or 0) / GB,
            (m.get("peak_bytes") or 0) / GB, m.get("mem_gb") or 0, age / 60, (m.get("owner") or "")[:36]))
    print("room for a new job now: %.1f GB" % (host_room(all_jobs()) / GB))


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
        if is_paused(m):
            cg_write(unit_cgroup(m) / "cgroup.freeze", 0)
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
    mx = a.max_gb or round(tot * 0.85)
    Path("/etc/systemd/system/agentjobs.slice").write_text(
        "# All agent batch jobs started with 'job run' (agentjobs-<project>.slice below this one).\n"
        "# The cap is for all of them together, so they cannot take the memory of services.\n"
        "[Unit]\nDescription=Agent batch jobs (job run)\nBefore=slices.target\n\n"
        "# No MemoryHigh: above it, with swap full, the kernel slows a job for hours and never kills it.\n"
        "# Normal CPU and IO weight: an agent that uses the queue must not run slower than one that does not.\n"
        "[Slice]\nMemoryMax=%dG\n" % mx)
    dst = "/usr/local/bin/job"
    if os.path.realpath(dst) != SELF:
        data = Path(SELF).read_bytes()
        Path(dst).write_bytes(data)
        os.chmod(dst, 0o755)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print("agentjobs.slice: MemoryMax=%dG of %.0f GB RAM; %s installed" % (mx, tot, dst))
    user = os.environ.get("SUDO_USER") or "root"
    Path("/etc/systemd/system/agentjobs-watch-%s.service" % user).write_text(
        "[Unit]\nDescription=Agent jobs memory watcher (%s)\n\n"
        "[Service]\nUser=%s\nExecStart=%s %s _watch\nRestart=always\nRestartSec=2\nOOMScoreAdjust=-900\n\n"
        "[Install]\nWantedBy=multi-user.target\n" % (user, user, sys.executable, dst))
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "agentjobs-watch-%s.service" % user], check=True)
    subprocess.run(["systemctl", "restart", "agentjobs-watch-%s.service" % user], check=True)
    print("agentjobs-watch-%s.service runs" % user)


def host_room(jobs):
    """Bytes a new job can have now. Linux: measured free memory (0 while the door is closed)."""
    if SYS != "Linux":
        return room(jobs)
    ok, _ = door({}, jobs)
    return max(0, free_now(jobs)) if ok else 0


def cmd_room(a):
    """One JSON line: the room for a new job on this host now."""
    jobs = all_jobs()
    n = os.cpu_count() or 1
    try:
        load = os.getloadavg()[0]
    except (OSError, AttributeError):
        load = 0.0
    q = [m for m in jobs if m.get("state") == "queued"]
    print(json.dumps({"host": platform.node(), "room_gb": round(host_room(jobs) / GB, 1), "cores": n, "load": round(load, 1),
                      "running": sum(1 for m in jobs if m.get("state") == "run" and not is_paused(m)),
                      "paused": sum(1 for m in jobs if is_paused(m)), "queued": len(q),
                      "oldest_wait_min": round((now() - min(m["created"] for m in q)) / 60) if q else 0,
                      "events_24h": event_counts()}))


def better_host(given):
    """(name, row, load, cores) of another job host that has free cores and memory room now, when this host has
    none: the load is above CPU_X times the cores. None when this host is fine, when no other host is better, or
    when the peers file is old or missing (then every job starts here)."""
    try:
        load, n = os.getloadavg()[0], os.cpu_count() or 1
        if load <= CPU_X * n:
            return None
        f = STATE / "peers.json"
        if time.time() - f.stat().st_mtime > PEER_STALE_S:
            return None
        rows = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    need = max(START_GB, float(given or 0) / 2)
    best = None
    for name, r in rows.items():
        if not r or r.get("host") == platform.node() or r.get("queued") or r.get("paused"):
            continue
        free = r.get("cores", 0) - r.get("load", 1e9)
        if free >= PEER_FREE and r.get("room_gb", 0) >= need and (not best or free > best[1].get("cores", 0) - best[1].get("load", 0)):
            best = (name, r, load, n)
    return best


def cmd_peers(a):
    """Ask each job host for its room ('job where') and give the answers to each host as <state>/peers.json.
    Run it each minute from one machine that can reach all hosts. Hosts use it to send jobs to a host with free cores."""
    from concurrent.futures import ThreadPoolExecutor
    f = STATE / "hosts"
    hosts = (os.environ.get("AGENTJOBS_HOSTS") or (f.read_text() if f.exists() else "")).split()
    opt = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6"]

    def ask(h):
        try:
            out = subprocess.run(opt + [h, "job room"], capture_output=True, text=True, timeout=20).stdout
            return h, json.loads(out.strip().splitlines()[-1])
        except Exception:
            return h, None

    def give(h):
        try:
            subprocess.run(opt + [h, "cat > ~/.agentjobs/peers.tmp && mv ~/.agentjobs/peers.tmp ~/.agentjobs/peers.json"],
                           input=data, text=True, capture_output=True, timeout=20)
        except Exception:
            pass
    if not hosts:
        return
    with ThreadPoolExecutor(len(hosts)) as ex:
        rows = dict(ex.map(ask, hosts))
        data = json.dumps(rows)
        list(ex.map(give, [h for h in hosts if rows.get(h)]))


def cmd_where(a):
    """Show the room on each job host, most room first. Hosts: AGENTJOBS_HOSTS or the file <state>/hosts."""
    from concurrent.futures import ThreadPoolExecutor
    f = STATE / "hosts"
    hosts = (os.environ.get("AGENTJOBS_HOSTS") or (f.read_text() if f.exists() else "")).split()
    if not hosts:
        sys.exit("job where: no hosts. Put host names in %s (one per line) or set AGENTJOBS_HOSTS." % f)

    def ask(h):
        try:
            out = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", h, "job room"],
                                 capture_output=True, text=True, timeout=20).stdout
            return h, json.loads(out.strip().splitlines()[-1])
        except Exception:
            return h, None
    with ThreadPoolExecutor(len(hosts)) as ex:
        rows = list(ex.map(ask, hosts))
    if a.json:
        print(json.dumps({h: r for h, r in rows}))
        return
    print("%-12s %8s %7s %7s %6s %10s  %s" % ("HOST", "ROOM", "QUEUED", "RUNNING", "CORES", "FREE CORES", "LAST 24 H"))
    for h, r in sorted(rows, key=lambda x: -(x[1] or {}).get("room_gb", -1e9)):
        if not r:
            print("%-12s %8s" % (h, "no answer"))
            continue
        ev = r.get("events_24h") or {}
        print("%-12s %7.1fG %7d %7d %6d %10.0f  %d paused, %d killed, %d rejected" % (
            h, r["room_gb"], r["queued"], r["running"], r["cores"], max(0, r["cores"] - r["load"]),
            ev.get("pause", 0), ev.get("kill", 0), ev.get("reject", 0)))


def main():
    ap = argparse.ArgumentParser(prog="job", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="sub", required=True)
    r = sp.add_parser("run")
    r.add_argument("--project")
    r.add_argument("--name")
    r.add_argument("--owner")
    r.add_argument("--cwd")
    r.add_argument("--peak-gb", type=float, help="your guess of the peak memory, for a new kind of job (default 2); "
                   "later runs with the same project and name use the measured peak")
    r.add_argument("--mem-gb", type=float, help="hard cap; default 3x peak, at least 2")
    r.add_argument("--cpus", type=int, help="math threads; on Mac, reserved CPU slots (default 1, host limit cores minus 4)")
    r.add_argument("--swap-gb", type=float, default=1.0, help="swap the job may use (Linux)")
    r.add_argument("--wait", action="store_true", help="stay in the foreground: print the log, exit with the job's code")
    r.add_argument("--queue", action="store_true", help="Linux: when the host is full, wait here (default: exit 75, job not started)")
    r.add_argument("--no-pause", action="store_true", help="Linux: never pause or kill this job for memory (services; "
                   "jobs that hold a lock or a connection that others need)")
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
    i.add_argument("--max-gb", type=float)
    sv = sp.add_parser("_supervise")
    sv.add_argument("id")
    sp.add_parser("_requeue")
    sp.add_parser("_watch")
    sp.add_parser("_peers")
    sp.add_parser("room")
    wh = sp.add_parser("where")
    wh.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.sub in ("ls", "where", "logs", "room") and hasattr(signal, "SIGPIPE"):
        # "job ls | head" closed the pipe and printed a BrokenPipeError traceback (alphas workers, Nov 24)
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    {"run": cmd_run, "ls": cmd_ls, "logs": cmd_logs, "wait": cmd_wait, "stop": cmd_stop, "install": cmd_install,
     "room": cmd_room, "where": cmd_where, "_watch": cmd_watch, "_peers": cmd_peers,
     "_supervise": lambda a: supervise(a.id), "_requeue": cmd_requeue}[a.sub](a)


if __name__ == "__main__":
    main()

