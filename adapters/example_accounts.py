#!/usr/bin/env python3
"""Example "command" account source. Copy it, replace collect() with calls to your router, proxy or
provider API, and add it to config.toml:

  [[accounts]]
  type = "command"
  name = "my-router"
  command = ["python3", "adapters/my_router.py"]

Print one JSON object. See docs/plugins.md for every field."""
import json
import os
import time


def collect(refresh):
    week = 7 * 86400
    return {
        "accounts": [
            {
                "provider": "openai",                  # a key of [providers]: openai, anthropic, google, xai, or your own
                "id": "example-1",                     # stable: history is kept by id
                "label": "me@example.com",             # what the page shows
                "plan": "pro",                         # used with plan_weights to size the account in the pool
                "weekly_used": 42.5,                   # % of the weekly limit used
                "weekly_reset": time.time() + 2 * 86400,  # epoch seconds, epoch ms or ISO 8601
                "short_used": 10,                      # optional: % of a short window (for example 5 hours)
                "short_reset": time.time() + 3600,
                "status": "ok",                        # ok | signed_out | error | no_quota
                "active": True,                        # optional: the account in use now
                "resets": [{"label": "Bonus reset", "expires": time.time() + week, "weekly": True}],  # optional
            }
        ],
        "issues": [],                                  # optional: [{"level": "warn", "text": "router is slow"}]
    }


if __name__ == "__main__":
    print(json.dumps(collect(os.environ.get("FLEET_REFRESH") == "1")))
