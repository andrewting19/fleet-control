"""Title sources: names for agent sessions. Each returns {session id: {"title", "group", "group_title", "client"}}.
"group" joins many sessions into one thread (for example, one app thread that ran several sessions)."""
import json
import os
import sqlite3


def codex_index(conf):
    """Thread names that Codex saves in ~/.codex/session_index.jsonl."""
    path = os.path.expanduser(os.path.join(conf.get("codex_home") or "~/.codex", "session_index.jsonl"))
    out = {}
    try:
        with open(path, errors="ignore") as f:
            for ln in f:
                try:
                    o = json.loads(ln)
                except ValueError:
                    continue
                if o.get("thread_name"):
                    out[o["id"]] = {"title": o["thread_name"]}
    except OSError:
        pass
    return out


def t3_code(conf):
    """Thread names from T3 Code (https://github.com/pingdotgg/t3code). Forked threads join their root thread."""
    path = os.path.expanduser(conf.get("db") or "~/.t3/userdata/statev2.sqlite")
    if not os.path.exists(path):
        return {}
    db = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    try:
        threads = {tid: (title, parent) for tid, title, parent in db.execute(
            "select thread_id, title, json_extract(payload_json,'$.lineage.parentThreadId') from orchestration_v2_projection_threads")}
        out = {}
        for ptid, tid in db.execute("select provider_thread_id, thread_id from orchestration_v2_projection_provider_threads"):
            if not tid or "native-thread:" not in ptid:
                continue
            native = ptid.split("native-thread:", 1)[1]
            if native.startswith("pending"):
                continue
            root, hops = tid, 0
            while threads.get(root, (None, None))[1] and hops < 10:
                root, hops = threads[root][1], hops + 1
            out[native] = {"title": threads.get(tid, ("",))[0], "group": "t3:" + root,
                           "group_title": threads.get(root, ("",))[0], "client": "T3 Code"}
        return out
    finally:
        db.close()
