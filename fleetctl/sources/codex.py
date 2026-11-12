"""Built-in GPT account source for the Codex CLI and app. Codex writes the account's rate limits into each
session file, so this reads the newest session file and ~/.codex/auth.json. No network calls.
Config: [[accounts]] type = "codex", optional codex_home = "~/.codex"."""
import base64
import glob
import json
import os
import time

WEEK_MIN = 7 * 24 * 60


def _jwt_claims(token):
    try:
        body = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (IndexError, ValueError):
        return {}


def _roll(window, now):
    """A limit read some time ago may have reset since then. Move it to the current window."""
    if not window:
        return None, None
    used, reset, mins = window.get("used_percent"), window.get("resets_at"), window.get("window_minutes") or WEEK_MIN
    if reset and reset < now:
        while reset < now:
            reset += mins * 60
        used = 0.0
    return used, reset


class CodexSource:
    def __init__(self, conf, cfg, store):
        self.home = os.path.expanduser(conf.get("codex_home") or os.environ.get("CODEX_HOME") or "~/.codex")

    def latest_limits(self):
        files = sorted(glob.glob(os.path.join(self.home, "sessions", "*", "*", "*", "*.jsonl")), key=os.path.getmtime, reverse=True)
        for path in files[:20]:
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - 2_000_000))
                lines = f.read().split(b"\n")
            for raw in reversed(lines):
                if b'"rate_limits"' not in raw:
                    continue
                try:
                    rl = (json.loads(raw).get("payload") or {}).get("rate_limits")
                except ValueError:
                    continue
                if rl and (rl.get("primary") or rl.get("secondary")):
                    return rl
        return None

    def collect(self, refresh):
        try:
            with open(os.path.join(self.home, "auth.json")) as f:
                auth = json.load(f)
        except OSError:
            return {"accounts": []}
        claims = _jwt_claims((auth.get("tokens") or {}).get("id_token") or "")
        info = claims.get("https://api.openai.com/auth") or {}
        email = claims.get("email") or "Codex"
        rl = self.latest_limits() or {}
        now = time.time()
        weekly = short = None
        for w in (rl.get("primary"), rl.get("secondary")):
            if not w:
                continue
            if (w.get("window_minutes") or WEEK_MIN) >= 24 * 60:
                weekly = w
            else:
                short = w
        wu, wr = _roll(weekly, now)
        su, sr = _roll(short, now)
        credits = rl.get("credits") or {}
        acct = {"provider": "openai", "id": "codex:" + (info.get("chatgpt_account_id") or email), "label": email,
                "plan": rl.get("plan_type") or info.get("chatgpt_plan_type"), "weekly_used": wu, "weekly_reset": wr,
                "short_used": su, "short_reset": sr, "active": True,
                "status": "ok" if weekly else "no_quota", "credits": float(credits["balance"]) if credits.get("balance") else None}
        if not auth.get("tokens"):
            acct.update(status="signed_out", status_text="no ChatGPT login in %s" % self.home)
        return {"accounts": [acct]}
