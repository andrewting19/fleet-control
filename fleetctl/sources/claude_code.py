"""Built-in Claude account source for Claude Code subscriptions (experimental).
It reads the Claude Code login (macOS keychain or ~/.claude/.credentials.json) and asks Anthropic's usage
endpoint for the 5-hour and weekly limits. The endpoint is the one Claude Code uses for /usage; it is not a
public API, so it can change. Config: [[accounts]] type = "claude_code"."""
import json
import os
import subprocess
import sys
import time
import urllib.request

URL = "https://api.anthropic.com/api/oauth/usage"


def _credentials():
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                                 capture_output=True, timeout=10).stdout
            if out.strip():
                return json.loads(out)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    try:
        with open(os.path.expanduser("~/.claude/.credentials.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class ClaudeCodeSource:
    def __init__(self, conf, cfg, store):
        self.conf = conf

    def collect(self, refresh):
        cred = (_credentials() or {}).get("claudeAiOauth") or {}
        try:
            with open(os.path.expanduser("~/.claude.json")) as f:
                email = (json.load(f).get("oauthAccount") or {}).get("emailAddress")
        except (OSError, ValueError):
            email = None
        acct = {"provider": "anthropic", "id": "claude_code:" + (email or "default"), "label": email or "Claude Code",
                "plan": cred.get("subscriptionType"), "active": True}
        if not cred.get("accessToken"):
            acct.update(status="signed_out", status_text="no Claude Code login found")
            return {"accounts": [acct]}
        if cred.get("expiresAt") and cred["expiresAt"] / 1000 < time.time():
            acct.update(status="no_quota", status_text="login token expired; open Claude Code once to refresh it")
            return {"accounts": [acct]}
        req = urllib.request.Request(URL, headers={"Authorization": "Bearer " + cred["accessToken"],
                                                   "anthropic-beta": "oauth-2025-04-20", "User-Agent": "fleet-control"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.load(r)
        except urllib.error.HTTPError as e:
            acct.update(status="signed_out" if e.code == 401 else "no_quota", status_text="usage endpoint: HTTP %d" % e.code)
            return {"accounts": [acct]}
        week, five = d.get("seven_day") or {}, d.get("five_hour") or {}
        acct.update(weekly_used=week.get("utilization"), weekly_reset=week.get("resets_at"),
                    short_used=five.get("utilization"), short_reset=five.get("resets_at"), status="ok" if week else "no_quota")
        return {"accounts": [acct]}
