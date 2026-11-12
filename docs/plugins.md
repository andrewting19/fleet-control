# Extending Fleet Control

Fleet Control has four places to plug in your own setup. Most setups need only config.toml.

| What | Where | How |
| --- | --- | --- |
| Machines | config.toml [[hosts]] | local, SSH alias, Windows over SSH, or Tailscale status only |
| Model accounts and quota | config.toml [[accounts]] | built-in source, or any program that prints JSON |
| Thread names | config.toml [[titles]] | built-in source, or a Python function |
| Token use | probes/agent_scan.py | add a reader for another agent's logs |

## Account sources

An account source returns the accounts of one or more providers and their weekly quota.

### Command (any language)

Use this for a router, a proxy, or a provider API. The program prints one JSON object to stdout.
It runs every 2 minutes, with FLEET_REFRESH=1 every 10 minutes (and on the Refresh button) when it should
ask its upstream for fresh numbers. A non-zero exit code shows as a collector problem on the page.
Start from adapters/example_accounts.py.

```json
{
  "accounts": [
    {
      "provider": "anthropic",
      "id": "acct-123",
      "label": "me@example.com",
      "plan": "max_20x",
      "weekly_used": 55,
      "weekly_reset": "2030-01-08T10:00:00Z",
      "short_used": 12,
      "short_reset": 1893459600,
      "status": "ok",
      "status_text": null,
      "active": true,
      "credits": null,
      "resets": [{"label": "Weekly reset", "expires": 1893888000, "weekly": true, "usable": true}]
    }
  ],
  "issues": [{"level": "crit", "area": "service", "text": "router does not answer"}]
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| provider | yes | key of [providers]. Built in: openai, anthropic, google, xai. A new key works too; add a name and color under [providers.KEY] |
| id | yes | stable id. Usage history is kept by id |
| label | no | name on the page, often the login email (default: id) |
| plan | no | plan name. plan_weights of the provider size the account in the pooled chart (default weight 1) |
| weekly_used | no | % of the weekly limit used. Without it, the account shows "no quota data" |
| weekly_reset | no | time of the next weekly reset |
| short_used, short_reset | no | a shorter window, for example 5 hours |
| status | no | ok, signed_out, error or no_quota. signed_out is a critical problem |
| status_text | no | detail for the status |
| active | no | true for the account in use now |
| credits | no | paid credit balance |
| resets | no | extra resets that the user can spend: label, expires, weekly (clears the weekly limit), usable |
| issues | no | level (crit, warn, info), area, text, who, fix (a command the user can copy) |

Times can be epoch seconds, epoch milliseconds or ISO 8601.

### Python

For a source inside the package, add a module in fleetctl/sources/ with a class:

```python
class MySource:
    def __init__(self, conf, cfg, store):   # conf: its [[accounts]] table
        ...
    def collect(self, refresh):             # same JSON shape as above, as a dict
        return {"accounts": [...], "issues": [...]}
```

Register it in ACCOUNT_SOURCES in fleetctl/sources/__init__.py. See codex.py for a small one.

## Title sources

A title source gives names to agent sessions. It is a function conf -> {session id: info}:

```python
def my_app(conf):
    return {"019a...": {"title": "Fix login bug", "group": "thread-7", "group_title": "Login work", "client": "My App"}}
```

group is optional: sessions with the same group show as one thread. Register it in TITLE_SOURCES.

## Token use from other agents

probes/agent_scan.py runs on each host with scan = true. It reads Codex (~/.codex/sessions) and
Claude Code (~/.claude/projects) logs incrementally and prints sessions with hourly token buckets:

```json
{"src": "codex", "sid": "...", "cwd": "/path", "title": "...", "parent": null, "model": "...",
 "first": 1893450000, "last": 1893456000,
 "bk": {"1893452400": {"model-name": [uncached_input, cache_read, cache_write, output, requests]}}}
```

To add another agent: write a scan_NAME(path, fs) function like scan_claude, and list its files in candidates().
The script must stay one file with only the standard library and must run on Python 3.6, because it is
streamed over SSH to each host. Model names map to a provider by [providers.*].match and to a price by
fleetctl/prices.toml.

## Machines

probes/host_probe.py (Linux and macOS) and probes/win_probe.ps1 (Windows) print one JSON object. To support
another OS, make a probe that prints the same keys: cores, load, cpu_util, mem_total, mem_avail, swap_total,
swap_used, disks, procs, mem_top. See host_probe.py for the full list.
