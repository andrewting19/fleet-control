"""Account source that runs any program and reads its JSON from stdout. Use it to connect a router,
a proxy or a script in any language. Config:
  [[accounts]]
  type = "command"
  command = ["python3", "adapters/my_accounts.py"]   # or a shell string
  timeout = 60
The program gets FLEET_REFRESH=1 in its environment when the user asks for fresh numbers.
It prints {"accounts": [...], "issues": [...]}; see docs/plugins.md for the fields."""
import json
import os
import subprocess

from ..config import ROOT


class CommandSource:
    def __init__(self, conf, cfg, store):
        if not conf.get("command"):
            raise ValueError("a command account source needs command = [...]")
        self.cmd, self.timeout = conf["command"], conf.get("timeout", 60)

    def collect(self, refresh):
        env = dict(os.environ, FLEET_REFRESH="1" if refresh else "0")
        p = subprocess.run(self.cmd, shell=isinstance(self.cmd, str), capture_output=True, timeout=self.timeout, env=env, cwd=ROOT)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.decode("utf-8", "replace").strip()[-300:] or "exit %d" % p.returncode)
        return json.loads(p.stdout)
