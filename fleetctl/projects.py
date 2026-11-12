"""Map a working folder (or a command line) to a project name and an optional sub-project."""
import os
import re


class Projects:
    """Rules come from config.toml ([[projects]]), then the built-in defaults. The first match wins.
    A rule: match = regex; project = fixed name, or the named group (?P<p>...); sub = fixed, or the group (?P<s>...).
    cmd = true also tries the rule on the process command line when the folder does not match."""

    def __init__(self, rules):
        self.rules = [(re.compile(r["match"]), r.get("project"), r.get("sub"), r.get("cmd", False)) for r in rules]
        self.home = os.path.expanduser("~")

    def __call__(self, path, cmd=""):
        path = path or ""
        for rx, proj, sub, _ in self.rules:
            m = rx.search(path)
            if m:
                return self._name(m, proj, sub)
        for rx, proj, sub, use_cmd in self.rules:
            if use_cmd and cmd:
                m = rx.search(cmd)
                if m:
                    return self._name(m, proj, sub)
        if not path or path == "/" or path in (self.home, "/root") or re.match(r"^/home/[^/]+/?$", path):
            return "system", ""
        return "other", ""

    @staticmethod
    def _name(m, proj, sub):
        g = m.groupdict()
        return proj or g.get("p") or "other", sub if sub is not None else (g.get("s") or "")
