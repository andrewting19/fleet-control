#!/usr/bin/env python3
"""Fleet Control agent-usage scan. Reads Codex rollouts (~/.codex/sessions) and Claude Code
transcripts (~/.claude/projects) and prints token use per session in hourly buckets.
It is incremental: it keeps a byte offset per file in ~/.cache/fleet-control/scan-state.json,
so later runs read only new lines. Bucket values: [uncached_in, cache_read, cache_write, out, requests, cache_write_1h]
(cache_write_1h is the part of cache_write that went to the 1-hour cache).
A request with a large prompt is counted under "<model>@L<k>" (for example gpt-6.1-sol@L272): its whole input
(uncached + cache read + cache write) was over k thousand tokens. Some models cost more per token above such a size."""
import datetime, glob, json, os, sys, time

KEEP = 8 * 86400
STATE = os.path.expanduser("~/.cache/fleet-control/scan-state.json")
HOME = os.path.expanduser("~")


def ts_of(s):
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def load_state():
    try:
        with open(STATE) as f:
            st = json.load(f)
        if st.get("v") == 5:
            return st
    except Exception:
        pass
    return {"v": 5, "files": {}}


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, separators=(",", ":"))
    os.replace(tmp, STATE)


LONG = (272, 200, 100)  # prompt sizes (thousand tokens) where providers start a higher price tier


def add(bk, t, model, unc, cr, cw, out, cw1h=0):
    size = unc + cr + cw
    for k in LONG:
        if size > k * 1000:
            model = "%s@L%d" % (model, k)
            break
    h = bk.setdefault(str(int(t // 3600) * 3600), {})
    v = h.setdefault(model, [0, 0, 0, 0, 0, 0])
    v[0] += unc; v[1] += cr; v[2] += cw; v[3] += out; v[4] += 1; v[5] += cw1h


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") in ("text", "input_text") and c.get("text"):
                return c["text"]
    return ""


def read_new(path, fs):
    size = os.path.getsize(path)
    off = fs.get("off", 0)
    if size < off:
        off = 0
        for k in ("cum", "bk", "seen"):
            fs.pop(k, None)
    if size == off:
        return []
    with open(path, "rb") as f:
        f.seek(off)
        data = f.read(size - off)
    end = data.rfind(b"\n")
    if end < 0:
        return []
    fs["off"] = off + end + 1
    return data[:end].split(b"\n")


def scan_codex(path, fs):
    bk = fs.setdefault("bk", {})
    for raw in read_new(path, fs):
        if b'"token_count"' in raw:
            try:
                o = json.loads(raw)
            except Exception:
                continue
            info = (o.get("payload") or {}).get("info") or {}
            tot = info.get("total_token_usage") or {}
            last = info.get("last_token_usage") or {}
            t = ts_of(o.get("timestamp", ""))
            if not t or not tot:
                continue
            cum = fs.get("cum")
            cur = [tot.get("input_tokens", 0), tot.get("cached_input_tokens", 0), tot.get("output_tokens", 0)]
            if cum and cur[0] >= cum[0] and cur[2] >= cum[2]:
                d = [cur[0] - cum[0], cur[1] - cum[1], cur[2] - cum[2]]
            elif cum:
                d = [last.get("input_tokens", 0), last.get("cached_input_tokens", 0), last.get("output_tokens", 0)]
            else:
                d = cur
            fs["cum"] = cur
            if d[0] + d[2] <= 0:
                continue
            cached = max(0, min(d[1], d[0]))
            add(bk, t, fs.get("model") or "unknown", d[0] - cached, cached, 0, d[2])
            fs["last"] = t
            fs.setdefault("first", t)
        elif b'"turn_context"' in raw[:120]:
            try:
                p = json.loads(raw).get("payload") or {}
            except Exception:
                continue
            if p.get("model"):
                fs["model"] = p["model"]
            if p.get("cwd"):
                fs["cwd"] = p["cwd"]
        elif b'"session_meta"' in raw[:120]:
            try:
                p = json.loads(raw).get("payload") or {}
            except Exception:
                continue
            fs["sid"] = p.get("id") or fs.get("sid")
            fs["cwd"] = p.get("cwd") or fs.get("cwd")
            fs["orig"] = p.get("originator") or ""
            src = p.get("source")
            if isinstance(src, dict):
                sub = src.get("subagent")
                if isinstance(sub, dict):
                    sp = sub.get("thread_spawn") or {}
                    fs["parent"] = sp.get("parent_thread_id") or fs.get("parent")
                    fs["role"] = sp.get("agent_nickname") or sp.get("agent_role") or "subagent"
                elif sub:
                    fs["role"] = str(sub)[:40]
        elif "title" not in fs and b'"user_message"' in raw[:200]:
            try:
                p = json.loads(raw).get("payload") or {}
            except Exception:
                continue
            if p.get("type") == "user_message":
                tx = (p.get("message") or "").strip()
                if tx and not tx.startswith(("#", "<")):
                    fs["title"] = tx.replace("\n", " ")[:120]
        elif "title" not in fs and b'"role":"user"' in raw[:300] and b'"response_item"' in raw[:120]:
            try:
                p = json.loads(raw).get("payload") or {}
            except Exception:
                continue
            tx = text_of(p.get("content")).strip()
            if tx and not tx.startswith(("#", "<")):
                fs["title"] = tx.replace("\n", " ")[:120]


def scan_claude(path, fs):
    bk = fs.setdefault("bk", {})
    seen = fs.setdefault("seen", [])
    seen_set = set(seen)
    for raw in read_new(path, fs):
        if b'"usage"' in raw and b'"assistant"' in raw:
            try:
                o = json.loads(raw)
            except Exception:
                continue
            m = o.get("message") or {}
            u = m.get("usage") or {}
            model = m.get("model") or ""
            if not u or model.startswith("<"):
                continue
            key = (m.get("id") or "") + ":" + (o.get("requestId") or "")
            if key in seen_set:
                continue
            seen_set.add(key)
            seen.append(key)
            t = ts_of(o.get("timestamp", ""))
            if not t:
                continue
            if o.get("cwd"):
                fs["cwd"] = o["cwd"]
            fs["sid"] = o.get("sessionId") or fs.get("sid")
            fs["model"] = model
            cw = u.get("cache_creation_input_tokens", 0) or 0
            cw1h = min(cw, ((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0))
            add(bk, t, model, u.get("input_tokens", 0) or 0, u.get("cache_read_input_tokens", 0) or 0,
                cw, u.get("output_tokens", 0) or 0, cw1h)
            fs["last"] = t
            fs.setdefault("first", t)
        elif b'"type":"summary"' in raw[:60]:
            try:
                fs["title"] = (json.loads(raw).get("summary") or "")[:120]
            except Exception:
                pass
        elif "title" not in fs and b'"type":"user"' in raw:
            try:
                o = json.loads(raw)
            except Exception:
                continue
            if o.get("type") == "user" and not o.get("isMeta"):
                tx = text_of((o.get("message") or {}).get("content")).strip()
                if tx and not tx.startswith("<"):
                    fs["title"] = tx.replace("\n", " ")[:120]
                if o.get("cwd"):
                    fs["cwd"] = o["cwd"]
    if len(seen) > 4000:
        del seen[:-3000]


def candidates(now):
    out = []
    root = os.path.join(HOME, ".codex", "sessions")
    if os.path.isdir(root):
        for y in sorted(os.listdir(root))[-2:]:
            yp = os.path.join(root, y)
            if not os.path.isdir(yp):
                continue
            for mth in os.listdir(yp):
                mp = os.path.join(yp, mth)
                if not os.path.isdir(mp):
                    continue
                for d in os.listdir(mp):
                    dp = os.path.join(mp, d)
                    try:
                        if now - os.path.getmtime(dp) > 60 * 86400:
                            continue
                        out.extend(("codex", os.path.join(dp, fn)) for fn in os.listdir(dp) if fn.endswith(".jsonl"))
                    except OSError:
                        pass
    croot = os.path.join(HOME, ".claude", "projects")
    if os.path.isdir(croot):
        for p in glob.glob(os.path.join(croot, "*", "*.jsonl")) + glob.glob(os.path.join(croot, "*", "*", "subagents", "*.jsonl")):
            out.append(("claude", p))
    return out


def main():
    now = time.time()
    st = load_state()
    files = st["files"]
    live = set()
    for kind, path in candidates(now):
        try:
            mt = os.path.getmtime(path)
            size = os.path.getsize(path)
        except OSError:
            continue
        if now - mt > KEEP:
            continue
        live.add(path)
        fs = files.get(path)
        if fs is None:
            fs = files[path] = {"k": kind}
        if fs.get("off") == size:
            continue
        try:
            (scan_codex if kind == "codex" else scan_claude)(path, fs)
        except Exception as e:
            fs["err"] = str(e)[:200]
        if kind == "claude" and "/subagents/" in path:
            fs["parent"] = os.path.basename(os.path.dirname(os.path.dirname(path)))
            fs["role"] = "subagent"
    cutoff = int((now - KEEP) // 3600) * 3600
    for p in list(files):
        if p not in live:
            del files[p]
            continue
        bk = files[p].get("bk") or {}
        for h in [h for h in bk if int(h) < cutoff]:
            del bk[h]
    save_state(st)
    sessions = []
    for p, fs in files.items():
        if not fs.get("bk"):
            continue
        sessions.append({"src": fs["k"], "sid": fs.get("sid") or os.path.basename(p)[:-6], "cwd": fs.get("cwd") or "",
                         "title": fs.get("title") or "", "orig": fs.get("orig") or "", "parent": fs.get("parent"),
                         "role": fs.get("role"), "model": fs.get("model"), "first": fs.get("first"), "last": fs.get("last"),
                         "bk": fs["bk"]})
    print(json.dumps({"ok": True, "home": HOME, "ts": now, "scan_ms": int((time.time() - now) * 1000), "sessions": sessions},
                     separators=(",", ":")))


if __name__ == "__main__":
    main()
