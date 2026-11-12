"""Run: python3 -m unittest discover tests"""
import base64
import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "probes"))

from fleetctl import accounts, config  # noqa: E402
from fleetctl.app import App  # noqa: E402
from fleetctl.hosts import thrash_level  # noqa: E402
from fleetctl.projects import Projects  # noqa: E402
from fleetctl.sources.codex import CodexSource  # noqa: E402
from fleetctl.sources.command import CommandSource  # noqa: E402
from fleetctl.util import WEEK  # noqa: E402


def write_config(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    f.write(text)
    f.close()
    return f.name


class ConfigTest(unittest.TestCase):
    def test_defaults_without_file(self):
        cfg = config.load("/nonexistent/config.toml")
        self.assertEqual(cfg["hosts"][0]["name"], "local")
        self.assertIn("openai", cfg["providers"])

    def test_bad_host(self):
        with self.assertRaises(config.ConfigError):
            config.load(write_config('[[hosts]]\nname = "x"\n'))

    def test_duplicate_host(self):
        with self.assertRaises(config.ConfigError):
            config.load(write_config('[[hosts]]\nname = "a"\nlocal = true\n[[hosts]]\nname = "a"\nssh = "a"\n'))

    def test_provider_override_merges(self):
        cfg = config.load(write_config('[providers.openai]\nname = "OpenAI"\n[providers.mistral]\ncolor = "#ff7000"\n'))
        self.assertEqual(cfg["providers"]["openai"]["name"], "OpenAI")
        self.assertEqual(cfg["providers"]["openai"]["color"], "#10a37f")
        self.assertEqual(cfg["providers"]["mistral"]["match"], ["mistral"])


class ProjectsTest(unittest.TestCase):
    def setUp(self):
        cfg = config.load("/nonexistent")
        self.p = Projects([{"match": r"/work/(?P<p>[^/]+)/(?P<s>[^/]+)"}, {"match": "train.py", "project": "ml", "cmd": True}] + cfg["projects"])

    def test_rules(self):
        self.assertEqual(self.p("/home/u/work/api/auth"), ("api", "auth"))
        self.assertEqual(self.p("/Users/me/code/web-app/src"), ("web-app", ""))
        self.assertEqual(self.p("/Users/me/.codex/worktrees/1a2b/web-app"), ("web-app", ""))
        self.assertEqual(self.p("/usr/lib"), ("system", ""))
        self.assertEqual(self.p("/", "python train.py"), ("ml", ""))
        self.assertEqual(self.p(""), ("system", ""))


class ThrashTest(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(thrash_level("linux", 50, 0.0, {"some": 0.1, "full": 0}), "ok")
        self.assertEqual(thrash_level("linux", 5, 5.0, {"some": 24, "full": 1}), "warn")
        self.assertEqual(thrash_level("linux", 5, 5.0, {"some": 45, "full": 12}), "crit")
        self.assertEqual(thrash_level("macos", 20, 3.0), "warn")
        self.assertEqual(thrash_level("macos", 20, 0.1), "ok")
        self.assertEqual(thrash_level("windows", 40, 50.0), "ok")  # page reads with free memory are normal
        self.assertEqual(thrash_level("windows", 4, 30.0), "crit")


class AccountsTest(unittest.TestCase):
    def test_normalize(self):
        a = accounts.normalize({"provider": "openai", "id": "x", "weekly_used": "40", "weekly_reset": "2030-01-01T00:00:00Z",
                                "resets": [{"expires": 1893456000000}]}, "src")
        self.assertEqual(a["label"], "x")
        self.assertEqual(a["weekly_used"], 40.0)
        self.assertEqual(a["weekly_reset"], 1893456000.0)
        self.assertEqual(a["resets"][0]["expires"], 1893456000.0)
        with self.assertRaises(ValueError):
            accounts.normalize({"provider": "openai"}, "src")

    def test_pool_drains_closest_reset_first(self):
        now = 1_800_000_000
        prov = {"openai": {"plan_weights": {"plus": 1, "pro": 5}}}
        soon = {"provider": "openai", "id": "soon", "plan": "plus", "weekly_used": 50, "weekly_reset": now + 10 * 3600, "rate": 1.0,
                "past": [[now - 3600, 49], [now, 50]]}
        late = {"provider": "openai", "id": "late", "plan": "pro", "weekly_used": 0, "weekly_reset": now + 6 * 86400, "rate": 0.0,
                "past": [[now - 3600, 0], [now, 0]]}
        p = accounts.pools([soon, late], prov, now)["openai"]
        self.assertEqual(p["weights"], {"soon": 1.0, "late": 5.0})
        self.assertAlmostEqual(p["left_now"], 100 * (0.5 + 5) / 6, places=1)
        k = p["times"].index(next(t for t in p["times"] if t > now + 3 * 3600))
        self.assertLess(p["bands"]["soon"][k], p["bands"]["soon"][168])  # soon drains
        self.assertAlmostEqual(p["bands"]["late"][k], p["bands"]["late"][168], places=1)  # late untouched
        self.assertTrue(any(r[1] == "soon" for r in p["resets"]))


class SourcesTest(unittest.TestCase):
    def test_command_source(self):
        out = json.dumps({"accounts": [{"provider": "anthropic", "id": "a", "weekly_used": 10}]})
        src = CommandSource({"command": [sys.executable, "-c", "print(%r)" % out]}, {}, None)
        self.assertEqual(src.collect(False)["accounts"][0]["id"], "a")

    def test_codex_source(self):
        home = tempfile.mkdtemp()
        claims = {"email": "me@example.com", "https://api.openai.com/auth": {"chatgpt_plan_type": "plus", "chatgpt_account_id": "acc1"}}
        jwt = "x." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".y"
        with open(os.path.join(home, "auth.json"), "w") as f:
            json.dump({"tokens": {"id_token": jwt}}, f)
        os.makedirs(os.path.join(home, "sessions", "2030", "01", "01"))
        reset = time.time() + 86400
        ev = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": 30, "window_minutes": 300, "resets_at": reset - 80000},
            "secondary": {"used_percent": 61, "window_minutes": 10080, "resets_at": reset}}}}
        with open(os.path.join(home, "sessions", "2030", "01", "01", "r.jsonl"), "w") as f:
            f.write(json.dumps(ev) + "\n")
        a = CodexSource({"codex_home": home}, {}, None).collect(False)["accounts"][0]
        self.assertEqual((a["label"], a["plan"], a["weekly_used"], a["short_used"]), ("me@example.com", "plus", 61, 30))


class ScanTest(unittest.TestCase):
    def test_codex_and_claude_logs(self):
        import agent_scan
        home = tempfile.mkdtemp()
        agent_scan.HOME, agent_scan.STATE = home, os.path.join(home, "state.json")
        t = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        cdir = os.path.join(home, ".codex", "sessions", "2030", "01", "01")
        os.makedirs(cdir)
        with open(os.path.join(cdir, "rollout.jsonl"), "w") as f:
            for o in [{"timestamp": t, "type": "session_meta", "payload": {"id": "s1", "cwd": "/w/api"}},
                      {"timestamp": t, "type": "turn_context", "payload": {"model": "gpt-6.1-sol", "cwd": "/w/api"}},
                      {"timestamp": t, "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage":
                       {"input_tokens": 1000, "cached_input_tokens": 600, "output_tokens": 50}}}}]:
                f.write(json.dumps(o) + "\n")
        pdir = os.path.join(home, ".claude", "projects", "-w-web")
        os.makedirs(pdir)
        with open(os.path.join(pdir, "s2.jsonl"), "w") as f:
            msg = {"type": "assistant", "timestamp": t, "sessionId": "s2", "cwd": "/w/web", "requestId": "r1",
                   "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {"input_tokens": 10, "cache_read_input_tokens": 500,
                                                                               "cache_creation_input_tokens": 20, "output_tokens": 30}}}
            f.write(json.dumps(msg) + "\n" + json.dumps(msg) + "\n")  # duplicate lines count once
        st = agent_scan.load_state()
        for kind, path in agent_scan.candidates(time.time()):
            fs = st["files"].setdefault(path, {"k": kind})
            (agent_scan.scan_codex if kind == "codex" else agent_scan.scan_claude)(path, fs)
        by = {fs.get("sid"): fs for fs in st["files"].values()}
        cx = list(by["s1"]["bk"].values())[0]["gpt-6.1-sol"]
        cl = list(by["s2"]["bk"].values())[0]["claude-opus-5-5"]
        self.assertEqual(cx[:4], [400, 600, 0, 50])
        self.assertEqual(cl, [10, 500, 20, 30, 1])


class DemoTest(unittest.TestCase):
    def test_demo_state(self):
        cfg = config.load("/nonexistent")
        cfg["data_dir"] = tempfile.mkdtemp()
        app = App(cfg, demo=True)
        from fleetctl import demo
        demo.fill(app)
        app.build()
        s = json.loads(app.snapshot)
        self.assertEqual(set(s["pools"]), {"openai", "anthropic"})
        self.assertEqual({h["name"]: h["status"] for h in s["hosts"]}["build-server"], "warn")
        self.assertTrue(any("thrashing" in i["text"] for i in s["issues"]))
        self.assertGreater(len(s["usage"]["threads"]["7d"]), 5)


if __name__ == "__main__":
    unittest.main()
