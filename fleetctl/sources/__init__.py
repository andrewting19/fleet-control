"""Plugins. Account sources give model accounts and their quota. Title sources give thread names.

To add an account source, write a class with collect(refresh) -> {"accounts": [...], "issues": [...]}
and register it in ACCOUNT_SOURCES, or use type = "command" to run any program that prints that JSON.
See docs/plugins.md."""
from . import claude_code, codex, command, opencodex, titles

ACCOUNT_SOURCES = {
    "codex": codex.CodexSource,
    "claude_code": claude_code.ClaudeCodeSource,
    "command": command.CommandSource,
    "opencodex": opencodex.OpencodexSource,
}

TITLE_SOURCES = {
    "codex": titles.codex_index,
    "t3": titles.t3_code,
}


def account_sources(cfg, store):
    out = []
    for i, c in enumerate(cfg["accounts"]):
        cls = ACCOUNT_SOURCES.get(c["type"])
        if not cls:
            raise ValueError("unknown account source type %r (known: %s)" % (c["type"], ", ".join(ACCOUNT_SOURCES)))
        out.append((c.get("name") or "%s%s" % (c["type"], i or ""), cls(c, cfg, store)))
    return out


def title_sources(cfg):
    out = []
    for c in cfg["titles"]:
        fn = TITLE_SOURCES.get(c["type"])
        if not fn:
            raise ValueError("unknown title source type %r (known: %s)" % (c["type"], ", ".join(TITLE_SOURCES)))
        out.append((c, fn))
    return out
