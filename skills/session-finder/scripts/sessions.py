#!/usr/bin/env python3
"""Find, search, and read past Claude Code and Codex CLI sessions from their local logs.

Reads ~/.claude/projects/**/*.jsonl and ~/.codex/sessions/**/rollout-*.jsonl[.zst].
Read-only, standard library only, no network access.

  sessions.py list [--days N] [--project NAME] [--agent claude|codex] [--limit N] [--json]
  sessions.py search QUERY [same options]
  sessions.py show SESSION_ID [--last N] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
COMMAND_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
SEARCH_LIMIT = 50_000  # characters of prompt text kept per session for searching


def parse_time(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_jsonl(path: Path):
    if path.name.endswith(".zst"):
        if not shutil.which("zstd"):
            return
        try:
            data = subprocess.run(["zstd", "-dcq", str(path)], capture_output=True, timeout=60).stdout
        except (OSError, subprocess.TimeoutExpired):
            return
        lines = data.decode("utf-8", "replace").splitlines()
    else:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def new_session(sid: str, path: Path, agent: str) -> dict:
    return {"id": sid, "agent": agent, "path": str(path), "cwd": None, "branch": None, "title": None,
            "first_prompt": None, "last_prompt": None, "started": None, "updated": None,
            "prompts": 0, "replies": 0, "tokens_in": 0, "tokens_out": 0, "models": {}, "_search": []}


def touch(s: dict, ts: float | None) -> None:
    if ts:
        s["updated"] = max(s["updated"] or ts, ts)
        s["started"] = min(s["started"] or ts, ts)


def add_prompt(s: dict, text: str) -> None:
    s["prompts"] += 1
    s["first_prompt"] = s["first_prompt"] or text
    s["last_prompt"] = text
    if sum(map(len, s["_search"])) < SEARCH_LIMIT:
        s["_search"].append(text.lower()[:4000])


# ---------- Claude Code ----------

def claude_user_text(content) -> str | None:
    """Text the person typed, or None for tool results and injected context."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    else:
        return None
    text = text.strip()
    if text.startswith("<command-name>"):
        m = COMMAND_RE.search(text)
        return m.group(1).strip() if m else None
    if not text or (text.startswith("<") and not text.startswith("<pasted")):
        return None
    return text


def claude_sessions() -> list[dict]:
    root = CLAUDE_DIR / "projects"
    if not root.exists():
        return []
    out = []
    for path in root.glob("*/*.jsonl"):
        s = new_session(path.stem, path, "claude")
        ai_title = custom_title = None
        seen = {}
        for o in read_jsonl(path):
            t = o.get("type")
            touch(s, parse_time(o.get("timestamp")))
            s["cwd"] = s["cwd"] or o.get("cwd")  # resume only works from the starting folder
            s["branch"] = o.get("gitBranch") or s["branch"]
            if t == "ai-title" and o.get("aiTitle"):
                ai_title = o["aiTitle"]
            elif t == "custom-title" and o.get("customTitle"):
                custom_title = o["customTitle"]
            elif t == "summary" and o.get("summary") and not ai_title:
                ai_title = o["summary"]
            elif t == "user" and not o.get("isMeta") and not o.get("isSidechain"):
                text = claude_user_text((o.get("message") or {}).get("content"))
                if text:
                    add_prompt(s, text)
            elif t == "assistant" and isinstance(o.get("message"), dict) and not o.get("isSidechain"):
                msg = o["message"]
                usage, model = msg.get("usage"), msg.get("model") or ""
                if isinstance(usage, dict) and not model.startswith("<"):
                    # Streamed replies are logged several times; keep the last copy.
                    seen[(msg.get("id"), o.get("requestId"))] = (model, usage)
        for model, usage in seen.values():
            s["replies"] += 1
            io = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
            s["tokens_in"] += usage.get("input_tokens") or 0
            s["tokens_out"] += usage.get("output_tokens") or 0
            short = re.sub(r"-\d{8}$", "", model).replace("claude-", "")
            s["models"][short] = s["models"].get(short, 0) + io
        s["title"] = custom_title or ai_title
        out.append(s)
    return out


def claude_transcript(path: Path) -> list[dict]:
    items, tools = [], {}
    for o in read_jsonl(path):
        if o.get("isSidechain") or o.get("type") not in ("user", "assistant"):
            continue
        msg, ts = o.get("message") or {}, o.get("timestamp")
        if o["type"] == "user":
            content = msg.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in tools:
                        tools[b["tool_use_id"]]["error"] = bool(b.get("is_error"))
            text = None if o.get("isMeta") else claude_user_text(content)
            if text:
                items.append({"role": "user", "text": text, "ts": ts})
            continue
        for b in msg.get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip():
                if not items or items[-1].get("text") != b["text"]:
                    items.append({"role": "assistant", "text": b["text"], "ts": ts})
            elif b.get("type") == "tool_use" and b.get("id") not in tools:
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                tools[b["id"]] = {"role": "tool", "name": b.get("name"), "text": tool_summary(inp), "ts": ts}
                items.append(tools[b["id"]])
    return items


def tool_summary(args: dict) -> str:
    for key in ("command", "cmd", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
        v = args.get(key)
        if isinstance(v, list):
            v = " ".join(map(str, v))
        if isinstance(v, str) and v:
            return v.splitlines()[0][:200]
    return ""


# ---------- Codex CLI ----------

def codex_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("text"))
    return ""


def codex_prompt(text: str) -> str | None:
    text = (text or "").strip()
    if not text or text.startswith("<") or text.startswith("# AGENTS.md instructions"):
        return None
    return text


def codex_titles() -> dict:
    titles = {}
    for o in read_jsonl(CODEX_HOME / "session_index.jsonl"):
        if o.get("id") and o.get("thread_name"):
            titles[o["id"]] = o["thread_name"]
    return titles


def codex_sessions() -> list[dict]:
    root = CODEX_HOME / "sessions"
    if not root.exists():
        return []
    titles = codex_titles()
    by_id: dict[str, dict] = {}
    paths = [p for p in root.rglob("rollout-*") if p.name.endswith((".jsonl", ".jsonl.zst"))]
    plain = {p for p in paths if p.name.endswith(".jsonl")}
    for path in paths:
        if path.name.endswith(".zst") and path.with_name(path.name[:-4]) in plain:
            continue
        m = UUID_RE.search(path.name)
        if not m:
            continue
        s = by_id.setdefault(m.group(0), new_session(m.group(0), path, "codex"))
        model, last_total = None, 0
        for o in read_jsonl(path):
            t, p = o.get("type"), o.get("payload")
            if not isinstance(p, dict):
                continue
            touch(s, parse_time(o.get("timestamp")))
            if t == "session_meta":
                s["path"] = str(path)
                s["cwd"] = p.get("cwd") or s["cwd"]
                git = p.get("git") if isinstance(p.get("git"), dict) else {}
                s["branch"] = git.get("branch") or s["branch"]
            elif t == "turn_context":
                model = p.get("model") or model
                s["cwd"] = s["cwd"] or p.get("cwd")
            elif t == "response_item" and p.get("type") == "message":
                if p.get("role") == "user":
                    text = codex_prompt(codex_text(p.get("content")))
                    if text:
                        add_prompt(s, text)
                elif p.get("role") == "assistant":
                    s["replies"] += 1
            elif t == "event_msg" and p.get("type") == "token_count":
                info = p.get("info") if isinstance(p.get("info"), dict) else {}
                total = (info.get("total_token_usage") or {}).get("total_tokens") or 0
                last = info.get("last_token_usage") or {}
                if total > last_total:
                    last_total = total
                    inp = max(0, (last.get("input_tokens") or 0) - (last.get("cached_input_tokens") or 0))
                    out = last.get("output_tokens") or 0
                    s["tokens_in"] += inp
                    s["tokens_out"] += out
                    key = model or "codex"
                    s["models"][key] = s["models"].get(key, 0) + inp + out
        s["title"] = titles.get(s["id"])
    return list(by_id.values())


def codex_transcript(path: Path) -> list[dict]:
    items = []
    for o in read_jsonl(path):
        p, ts = o.get("payload"), o.get("timestamp")
        if o.get("type") != "response_item" or not isinstance(p, dict):
            continue
        kind = p.get("type")
        if kind == "message":
            text = codex_text(p.get("content"))
            if p.get("role") == "user":
                text = codex_prompt(text)
                if text:
                    items.append({"role": "user", "text": text, "ts": ts})
            elif p.get("role") == "assistant" and text.strip():
                items.append({"role": "assistant", "text": text, "ts": ts})
        elif kind in ("function_call", "custom_tool_call", "local_shell_call"):
            raw = p.get("arguments") if kind == "function_call" else p.get("input") or p.get("action")
            try:
                args = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except ValueError:
                args = {"input": raw}
            args = args if isinstance(args, dict) else {"input": args}
            items.append({"role": "tool", "name": p.get("name") or kind, "text": tool_summary(args), "ts": ts})
    return items


# ---------- output ----------

def resume_command(s: dict, fork: bool = False) -> str:
    cd = f"cd {shlex.quote(s['cwd'])} && " if s["cwd"] else ""
    if s["agent"] == "codex":
        return f"{cd}codex {'fork' if fork else 'resume'} {s['id']}"
    return f"{cd}claude --resume {s['id']}" + (" --fork-session" if fork else "")


def display_title(s: dict) -> str:
    t = s["title"] or s["first_prompt"] or "Untitled session"
    t = " ".join(t.split())
    return t if len(t) <= 90 else t[:87] + "..."


def when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"


def public(s: dict) -> dict:
    out = {k: v for k, v in s.items() if not k.startswith("_")}
    out["display_title"] = display_title(s)
    out["project"] = Path(s["cwd"]).name if s["cwd"] else None
    out["resume"] = resume_command(s)
    out["fork"] = resume_command(s, fork=True)
    return out


def all_sessions(agent: str | None) -> list[dict]:
    found = []
    if agent in (None, "claude"):
        found += claude_sessions()
    if agent in (None, "codex"):
        found += codex_sessions()
    found = [s for s in found if s["prompts"] or s["replies"]]
    found.sort(key=lambda s: -(s["updated"] or 0))
    return found


def matches(s: dict, query: str) -> bool:
    words = query.lower().split()
    hay = " ".join([s["title"] or "", s["cwd"] or "", s["branch"] or "", *s["_search"]]).lower()
    return all(w in hay for w in words)


def cmd_list(args, query: str | None = None) -> None:
    found = all_sessions(args.agent)
    if args.days:
        cutoff = time.time() - args.days * 86400
        found = [s for s in found if (s["updated"] or 0) >= cutoff]
    if args.project:
        p = args.project.lower()
        found = [s for s in found if s["cwd"] and p in s["cwd"].lower()]
    if query:
        found = [s for s in found if matches(s, query)]
    found = found[:args.limit]
    if args.json:
        print(json.dumps([public(s) for s in found], indent=2))
        return
    if not found:
        print("No sessions found.")
        return
    for s in found:
        project = Path(s["cwd"]).name if s["cwd"] else "?"
        branch = f" ({s['branch']})" if s["branch"] and s["branch"] != "HEAD" else ""
        print(f"{when(s['updated'])}  {s['agent']:<6}  {project}{branch}")
        print(f"  {display_title(s)}")
        print(f"  {s['prompts']} prompt{'' if s['prompts'] == 1 else 's'}, {s['tokens_in'] + s['tokens_out']:,} tokens  |  {resume_command(s)}")
        print()


def find_session(prefix: str) -> dict | None:
    hits = [s for s in all_sessions(None) if s["id"].startswith(prefix)]
    if len(hits) > 1:
        sys.exit(f"'{prefix}' matches {len(hits)} sessions; give more of the ID.")
    return hits[0] if hits else None


def cmd_show(args) -> None:
    s = find_session(args.id)
    if not s:
        sys.exit(f"No session with ID starting '{args.id}'.")
    path = Path(s["path"])
    items = (codex_transcript if s["agent"] == "codex" else claude_transcript)(path)[-args.last:]
    if args.json:
        print(json.dumps({"session": public(s), "items": items}, indent=2))
        return
    print(f"{display_title(s)}\n{s['agent']} session {s['id']}\n{s['cwd'] or '?'}  {when(s['started'])} to {when(s['updated'])}")
    print(f"Resume: {resume_command(s)}\nFork:   {resume_command(s, fork=True)}\n")
    for item in items:
        if item["role"] == "tool":
            print(f"  [{item['name']}] {item['text']}")
        else:
            text = item["text"].strip()
            if len(text) > 1500:
                text = text[:1500] + " ..."
            print(f"\n{'YOU' if item['role'] == 'user' else 'AGENT'}: {text}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Find and read past Claude Code and Codex sessions.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "search"):
        p = sub.add_parser(name)
        if name == "search":
            p.add_argument("query")
        p.add_argument("--days", type=int, help="only sessions active in the last N days")
        p.add_argument("--project", help="only sessions whose folder path contains this text")
        p.add_argument("--agent", choices=["claude", "codex"])
        p.add_argument("--limit", type=int, default=15)
        p.add_argument("--json", action="store_true")
    p = sub.add_parser("show")
    p.add_argument("id", help="session ID or a unique prefix of it")
    p.add_argument("--last", type=int, default=40, help="number of transcript items to print")
    p.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.cmd == "show":
        cmd_show(args)
    else:
        cmd_list(args, getattr(args, "query", None))


if __name__ == "__main__":
    main()
