# Instructions for coding agents

This file has two parts: how to set Fleet Control up for a user, and how to change the code.

## Part 1: set it up for the user

Goal: `config.toml` that covers the user's machines and accounts, `python3 fleet.py check` with no failures,
and the service running. Do the discovery yourself; ask the user only what you cannot find.

1. **Requirements.** `python3 --version` must be 3.11 or newer on this machine. Run
   `python3 fleet.py serve --demo --port 10499` for a few seconds if the user wants to see the page first.
2. **Find machines.**
   - Read `~/.ssh/config` for Host aliases. For each candidate, test `ssh -o BatchMode=yes -o ConnectTimeout=5 ALIAS true`.
     Only hosts with key login work.
   - For each reachable host, find its OS: `ssh ALIAS uname -s` (no answer or an error from uname on a host that
     runs PowerShell means Windows: test `ssh ALIAS powershell -Command '$PSVersionTable.PSVersion'`).
   - Linux and macOS hosts need python3 (any 3.6+).
   - If `tailscale status` works, hosts without SSH can be added as status-only with `tailscale = "NAME"`.
   - Ask the user which of the machines to show, and which are less important (`hidden = true`).
3. **Find agent logs.** A host gets `scan = true` if it has `~/.codex/sessions` or `~/.claude/projects`
   (test with `ssh ALIAS 'ls -d ~/.codex/sessions ~/.claude/projects'`).
4. **Find accounts.**
   - `~/.codex/auth.json` exists: add [[accounts]] `type = "codex"`.
   - Claude Code is logged in (macOS keychain item "Claude Code-credentials", or `~/.claude/.credentials.json`):
     add `type = "claude_code"`.
   - The user runs a router or proxy with many accounts (for example opencodex: `ocx account list --json`): use
     `type = "opencodex"`, or write a command source. Start from `adapters/example_accounts.py` and the contract in
     `docs/plugins.md`. Put user-specific adapters in `adapters/local/` (ignored by git).
   - Set plan names so the pooled chart weighs plans correctly. Check `plan_weights` in `fleetctl/config.py` and add
     the user's plans under `[providers.NAME].plan_weights` if they are missing.
5. **Write `config.toml`** from `config.example.toml`. Keep it short: only the keys that differ from the defaults.
6. **Check.** Run `python3 fleet.py check`. Fix each FAIL line and run it again until it reports no failures.
7. **Projects.** Run `python3 fleet.py projects`. If folders map to "other" or to a wrong project, add
   `[[projects]]` rules (first match wins; `(?P<p>...)` captures the project, `(?P<s>...)` a sub-project), then run it again.
8. **Run it.** `python3 fleet.py service install`. Wait about 60 seconds, then `curl -s localhost:10400/api/state` and
   check that hosts, accounts and usage are not empty. Tell the user the URL. If they want to open it from their
   phone over Tailscale, set `bind = "0.0.0.0"` (the default allow list admits only this machine and Tailscale).

Never spend resets, log in or out of accounts, or kill processes for the user as part of setup.

## Part 2: change the code

- **Layout.** `fleet.py` is the CLI. `fleetctl/` is the server: app.py (schedule and state), hosts.py, usage.py,
  accounts.py (pace and pooled projection), sources/ (account and title plugins), models.py and prices.toml,
  projects.py, config.py (defaults), demo.py. `probes/` run on the hosts. `ui/` is plain HTML, CSS and JS, with no build.
- **Rules.**
  - Standard library only. The server needs Python 3.11 (tomllib). Files in `probes/` must run on Python 3.6 and stay
    one file each, because they are streamed over SSH.
  - The UI gets everything from `GET /api/state`. Provider names, colors and icons come from `state.meta.providers`;
    do not hard-code providers in the UI. Icons are SVG masks in `ui/icons/` (class `ic ic-NAME`).
  - New config keys need a default in `fleetctl/config.py` and a line in `config.example.toml`.
  - Account sources only read. Do not add code that spends resets or changes accounts.
- **Test.** `python3 -m unittest discover tests`, then `python3 fleet.py serve --demo --port 10499` and look at the page
  (for example with headless Chrome: --headless=new --screenshot --window-size=1600,2400). Demo mode goes through
  the same code as real data, so it shows most UI breakage.
