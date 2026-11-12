"""Model names: normalize them, find their provider and their list price."""
import os
import re
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))


def norm(m):
    m = (m or "unknown").lower().split("/")[-1]
    return re.sub(r"\[1m\]$|-thinking$|-\d{8}$", "", m)


class Models:
    def __init__(self, providers, overrides=None):
        with open(os.path.join(HERE, "prices.toml"), "rb") as f:
            p = tomllib.load(f)
        self.prices = {k: tuple(v) for k, v in p["models"].items()}
        self.prices.update({k: tuple(v) for k, v in (overrides or {}).items()})
        self.families = p["families"]
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
        """v = [uncached input, cache read, cache write, output, requests] in tokens."""
        p = self.price(model)
        return (v[0] * p[0] + v[3] * p[1] + v[1] * p[2] + v[2] * p[3]) / 1e6
