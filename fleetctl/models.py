"""Model names: normalize them, find their provider and their list price."""
import os
import re
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))


def norm(m):
    m = (m or "unknown").lower().split("/")[-1].split("@")[0]
    return re.sub(r"\[1m\]$|-thinking$|-\d{8}$", "", m)


class Models:
    def __init__(self, providers, overrides=None):
        with open(os.path.join(HERE, "prices.toml"), "rb") as f:
            p = tomllib.load(f)
        self.prices = {k: tuple(v) for k, v in p["models"].items()}
        self.prices.update({k: tuple(v) for k, v in (overrides or {}).items()})
        self.families = p["families"]
        self.long = {k: tuple(v) for k, v in p.get("long", {}).items()}
        self.match = [(pid, re.compile("|".join(v["match"]))) for pid, v in providers.items() if v.get("match")]
        self._cache = {}

    def provider(self, model):
        n = norm(model)
        for pid, rx in self.match:
            if rx.search(n):
                return pid
        return "other"

    def price(self, model):
        n = norm(model)
        if n in self._cache:
            return self._cache[n]
        hit = self.prices.get(n)
        if hit is None:
            keys = sorted((k for k in self.prices if n.startswith(k)), key=len, reverse=True)
            hit = self.prices[keys[0]] if keys else None
        if hit is None:
            hit = next((self.prices[k] for f, k in self.families.items() if f in n and k in self.prices), (0, 0, 0, 0))
        self._cache[n] = hit
        return hit

    def cost(self, model, v):
        """v = [uncached input, cache read, cache write, output, requests, cache write 1h] in tokens.
        A model name that ends in @L<k> had a prompt over k thousand tokens; it gets the [long] price when
        the model's long tier starts at or below that size."""
        p = self.price(model)
        tier = re.search(r"@L(\d+)$", model or "")
        if tier:
            lt = self.long.get(norm(model))
            if lt and int(tier.group(1)) * 1000 >= lt[0]:
                p = lt[1:]
        w1h = p[4] if len(p) > 4 else p[3]
        cw1h = min(v[2], v[5]) if len(v) > 5 else 0
        unc = v[0] * p[0]
        if len(p) > 4 and not v[2]:
            # The log does not split cache writes from input (Codex logs). For Claude models, input that is not
            # read from the cache is nearly all written to the 1-hour cache (opencodex and Claude Code do this).
            unc = v[0] * w1h
        return (unc + v[3] * p[1] + v[1] * p[2] + (v[2] - cw1h) * p[3] + cw1h * w1h) / 1e6
