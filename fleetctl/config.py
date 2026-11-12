"""Load and check config.toml. Every key has a default, so a minimal config works.
See config.example.toml for all keys."""
import copy
import os
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    "title": "Fleet Control",
    "port": 10400,
    "bind": "127.0.0.1",
    # Client networks that may read the page. Default: this machine and Tailscale.
    "allow": ["127.0.0.0/8", "::1/128", "100.64.0.0/10", "fd7a:115c:a1e0::/48"],
    "data_dir": "data",
    "intervals": {"probe": 20, "scan": 120, "accounts": 120, "accounts_refresh": 600, "slow": 1800, "tailscale": 60},
    "thresholds": {"cpu_warn": 80, "cpu_crit": 95, "load_warn": 1.0, "load_crit": 1.5, "mem_free_warn": 12, "mem_free_crit": 5,
                   "disk_warn": 90, "disk_crit": 95, "iowait_warn": 30},
    "hosts": [{"name": "local", "local": True, "scan": True}],
    "tailscale": {"enabled": False, "cli": ""},
    "accounts": [{"type": "codex"}],
    "titles": [{"type": "codex"}],
    "providers": {},
    "projects": [],
    "project_defaults": True,
    "prices": {},
    "ui": {"project_colors": {}},
}

# Built-in providers. "match" is a list of regexes for model names. Plan weights size each plan's weekly
# limit in units of the smallest plan, so pooled charts weigh a big plan more than a small one.
PROVIDERS = {
    "openai": {"name": "GPT", "color": "#10a37f", "icon": "openai", "match": ["gpt", "codex", r"^o\d"],
               "plan_weights": {"plus": 1, "pro": 5, "promax": 20, "pro_max": 20}},
    "anthropic": {"name": "Claude", "color": "#d97757", "icon": "claude", "match": ["claude", "^opus", "^sonnet", "^haiku", "^fable"],
                  "plan_weights": {"pro": 1, "max_5x": 5, "max_20x": 10, "max": 5, "ultra": 10}},
    "google": {"name": "Gemini", "color": "#4796e3", "icon": "gemini", "match": ["gemini"], "plan_weights": {}},
    "xai": {"name": "Grok", "color": "#9ca3af", "icon": "x", "match": ["grok"], "plan_weights": {}},
}

# Used after the rules in config: worktrees of common agent apps, then common code folders.
PROJECT_DEFAULTS = [
    {"match": r"/\.(?:t3|codex|claude|conductor)/(?:projects|worktrees)/(?:[0-9a-f]{4,}/)?(?P<p>[^/]+)"},
    {"match": r"/(?:git_repos|repos|src|code|projects|dev|work|git)/(?P<p>[^/.][^/]*)"},
    {"match": r"/Library/|^/System/|^/Applications/|^/private/|^/var/|^/usr/|^/proc/|^/etc/|^/tmp|^/opt/homebrew", "project": "system"},
    {"match": r"^/(?:home/[^/]+|root|Users/[^/]+)/(?:Documents|Desktop|Downloads)/?$", "project": "system"},
    {"match": r"^/(?:home/[^/]+|root|Users/[^/]+)/(?P<p>[^/.][^/]*)"},
    {"match": r"^/(?:opt|srv)/(?P<p>[^/]+)"},
]


class ConfigError(Exception):
    pass


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(path=None):
    path = path or os.environ.get("FLEET_CONFIG") or os.path.join(ROOT, "config.toml")
    raw = {}
    if os.path.exists(path):
        with open(path, "rb") as f:
            try:
                raw = tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                raise ConfigError("%s: %s" % (path, e))
    cfg = _merge(DEFAULTS, raw)
    cfg["path"] = path if os.path.exists(path) else None
    cfg["providers"] = {k: _merge(PROVIDERS.get(k, {"name": k, "color": "#9b8cff", "icon": "", "match": [k], "plan_weights": {}}), v)
                        for k, v in {**{k: {} for k in PROVIDERS}, **raw.get("providers", {})}.items()}
    cfg["projects"] = list(cfg["projects"]) + (PROJECT_DEFAULTS if cfg["project_defaults"] else [])
    if not os.path.isabs(cfg["data_dir"]):
        cfg["data_dir"] = os.path.join(ROOT, cfg["data_dir"])
    check(cfg)
    return cfg


def check(cfg):
    names = set()
    for h in cfg["hosts"]:
        if not h.get("name"):
            raise ConfigError("every [[hosts]] entry needs a name")
        if h["name"] in names:
            raise ConfigError("host name %r is used twice" % h["name"])
        names.add(h["name"])
        if not (h.get("local") or h.get("ssh") or h.get("tailscale")):
            raise ConfigError("host %r needs local = true, ssh = \"alias\" or tailscale = \"name\"" % h["name"])
        if h.get("os", "posix") not in ("posix", "linux", "macos", "windows"):
            raise ConfigError("host %r: os must be linux, macos or windows" % h["name"])
    for a in cfg["accounts"]:
        if not a.get("type"):
            raise ConfigError("every [[accounts]] entry needs a type")
    for r in cfg["projects"]:
        if "match" not in r:
            raise ConfigError("every [[projects]] rule needs match")
