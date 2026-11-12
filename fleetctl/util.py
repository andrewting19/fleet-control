"""Small helpers shared by the collectors."""
import base64
import datetime
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBES = os.path.join(ROOT, "probes")
CACHE = os.path.expanduser("~/.cache/fleet-control")
WEEK = 7 * 86400


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def ssh_opts():
    os.makedirs(CACHE, exist_ok=True)
    # One shared connection per host, so a probe every 20 s does not pay for a new SSH handshake.
    return ["-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "ServerAliveInterval=10", "-o", "ControlMaster=auto",
            "-o", "ControlPath=" + os.path.join(CACHE, "cm-%C"), "-o", "ControlPersist=600"]


def run(cmd, timeout=60, stdin=None, env=None):
    p = subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout, env=env)
    if p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(err[-300:] or "exit %d" % p.returncode)
    return p.stdout


def last_json(out):
    """Parse the last line of output as JSON. Login banners and warnings come before it."""
    lines = out.decode("utf-8", "replace").strip().splitlines()
    if not lines:
        raise RuntimeError("no output")
    return json.loads(lines[-1])


def run_probe(host, script, timeout):
    """Run a probe script on a host and return its JSON. Local: python3. SSH: stream the script over stdin,
    so nothing is installed on the host. Windows: a PowerShell probe, sent as base64 so quoting is safe."""
    if host.get("os") == "windows":
        ps = open(os.path.join(PROBES, "win_probe.ps1"), "rb").read().decode("utf-8").encode("utf-16-le")
        line = "$s=[Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('%s')); Invoke-Expression $s\n" % base64.b64encode(ps).decode()
        return last_json(run(["ssh"] + ssh_opts() + [host["ssh"], "powershell", "-NoProfile", "-NonInteractive", "-Command", "-"],
                             timeout, stdin=line.encode()))
    path = os.path.join(PROBES, script)
    if host.get("local"):
        return last_json(run([sys.executable, path], timeout))
    with open(path, "rb") as f:
        return last_json(run(["ssh"] + ssh_opts() + [host["ssh"], "python3", "-"], timeout, stdin=f.read()))


def ts(v):
    """Epoch seconds from epoch seconds, epoch milliseconds or an ISO 8601 string."""
    if v is None or v == "":
        return None
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            pass
        try:
            return datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    v = float(v)
    return v / 1000 if v > 1e11 else v


def gb(b):
    return "%.1f GB" % (b / 1073741824) if b >= 1073741824 else "%d MB" % (b / 1048576)


def rel(sec):
    neg, sec = sec < 0, abs(sec)
    if sec < 3600:
        s = "%dm" % (sec // 60)
    elif sec < 86400:
        s = "%dh %dm" % (sec // 3600, (sec % 3600) // 60)
    else:
        s = "%dd %dh" % (sec // 86400, (sec % 86400) // 3600)
    return s + " ago" if neg else "in " + s
