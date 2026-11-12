"""Account source for opencodex (ocx), a local router that pools many ChatGPT and Claude logins.
Config:
  [[accounts]]
  type = "opencodex"
  ocx = "ocx"                         # path to the ocx CLI
  router_url = "http://127.0.0.1:10100/"   # optional: warn when the router does not answer
  remote_hosts = ["box"]              # optional: SSH aliases that run their own ocx; warn on logins that expired
  resets_every = 1800                 # seconds between reset-credit reads
  [accounts.labels]                   # optional: names for accounts whose labels ocx masks
  "__main__" = "me@example.com"       # key: account id, or its last 4 characters
The source only reads. It never spends resets."""
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from ..util import run, ssh_opts

PROVIDERS = ("openai", "anthropic")


class OpencodexSource:
    def __init__(self, conf, cfg, store):
        self.ocx = os.path.expanduser(conf.get("ocx") or shutil.which("ocx") or "ocx")
        self.labels = conf.get("labels") or {}
        self.router = conf.get("router_url")
        self.remote = conf.get("remote_hosts") or []
        self.resets_every = conf.get("resets_every", 1800)
        self.resets, self.resets_at, self.remote_bad = {}, 0, {}
        self.env = dict(os.environ, NODE_NO_WARNINGS="1")
        self.env["PATH"] = os.path.dirname(self.ocx) + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + self.env.get("PATH", "")

    def call(self, *args, timeout=120):
        p = subprocess.run([self.ocx] + list(args), capture_output=True, timeout=timeout, env=self.env)
        if p.returncode != 0:
            raise RuntimeError((p.stderr.decode("utf-8", "replace").strip().splitlines() or ["exit %d" % p.returncode])[-1][:300])
        return json.loads(p.stdout)

    def label(self, a):
        aid = a.get("slotId") or a.get("id") or ""
        return self.labels.get(aid) or self.labels.get(aid[-4:]) or self.unmask(a.get("email") or a.get("label")) or aid

    def unmask(self, label):
        """Match a masked label such as a***c@gmail.com to one configured label."""
        if not label or "***" not in label:
            return label
        local, _, dom = label.partition("@")
        hits = [e for e in set(self.labels.values()) if e.endswith("@" + dom) and e[0] == local[0] and e.split("@")[0][-1] == local[-1]]
        return hits[0] if len(hits) == 1 else label

    def collect(self, refresh):
        raw = {p: self.call("account", "list", p, "--quota", "--json", *(["--refresh"] if refresh else [])).get("accounts", [])
               for p in PROVIDERS}
        if time.time() - self.resets_at >= self.resets_every:
            self.read_resets(raw)
        credits = self.quota_cache()
        accounts = []
        for prov in PROVIDERS:
            for a in raw[prov]:
                q = a.get("quota") or {}
                aid = a.get("id") or a.get("slotId")
                bad = a.get("needsReauth") or a.get("health") not in (None, "Healthy")
                no_q = a.get("quotaUnavailable") or (a.get("quotaRefresh") or {}).get("status") not in (None, "ok")
                c = (credits.get(a.get("slotId") or aid) or {}).get("credits") or {}
                accounts.append({"provider": prov, "id": aid, "label": self.label(a), "plan": a.get("plan"),
                                 "weekly_used": q.get("weeklyPercent"), "weekly_reset": q.get("weeklyResetAt"),
                                 "short_used": q.get("shortPercent"), "short_reset": q.get("shortResetAt"), "active": a.get("active"),
                                 "status": "signed_out" if bad else "no_quota" if no_q else "ok",
                                 "status_text": a.get("health") if bad else None,
                                 "credits": float(c["balance"]) if c.get("balance") else None,
                                 "resets": self.resets.get(aid)})
        return {"accounts": accounts, "issues": self.router_issues() + self.remote_issues()}

    def read_resets(self, raw):
        out = {}
        for a in raw["openai"]:
            sid, aid = a.get("slotId") or a.get("id"), a.get("id") or a.get("slotId")
            try:
                cr = self.call("account", "reset-credits", "main" if sid == "__main__" else sid, "--json").get("credits", [])
                out[aid] = [{"label": "Codex reset credit", "expires": x.get("expires_at"), "weekly": True} for x in cr]
            except Exception:
                out[aid] = self.resets.get(aid)
        for a in raw["anthropic"]:
            try:
                d = self.call("account", "anthropic-reset-grants", a["id"], "--json")
                out[a["id"]] = [{"label": g.get("label"), "expires": g.get("endsAt"), "weekly": "seven_day" in (g.get("clears") or []),
                                 "usable": g.get("usableNow")} for g in d.get("grants") or [] for _ in range(int(g.get("resetsLeft") or 0))]
            except Exception:
                out[a["id"]] = self.resets.get(a["id"])
        self.resets, self.resets_at = out, time.time()

    def quota_cache(self):
        try:
            with open(os.path.expanduser("~/.opencodex/codex-quota-cache.json")) as f:
                return json.load(f).get("quotas", {})
        except (OSError, ValueError):
            return {}

    def router_issues(self):
        if not self.router:
            return []
        try:
            urllib.request.urlopen(self.router, timeout=5).read(64)
        except urllib.error.HTTPError:
            pass
        except Exception:
            return [{"level": "crit", "area": "service", "text": "opencodex router at %s does not answer" % self.router}]
        return []

    def remote_issues(self):
        out = []
        for host in self.remote:
            try:
                d = json.loads(run(["ssh"] + ssh_opts() + [host, "bash", "-lc", "'NODE_NO_WARNINGS=1 ocx account list --json 2>/dev/null'"], 60))
            except Exception as e:
                out.append({"level": "warn", "area": "service", "text": "%s: cannot read ocx accounts (%s)" % (host, str(e)[:80])})
                continue
            for a in d.get("accounts", []):
                if a.get("provider") in PROVIDERS and (a.get("needsReauth") or a.get("health") not in (None, "Healthy")):
                    out.append({"level": "crit", "area": "account", "provider": a["provider"],
                                "text": "%s: %s needs a new login" % (host, self.unmask(a.get("label")) or a.get("plan"))})
        return out
