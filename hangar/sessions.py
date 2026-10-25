"""Index of Claude Code sessions, built from the logs in ~/.claude/projects/<project>/<session>.jsonl.

Logs are append-only, so each file is read once and then only its new bytes are parsed.
Subagent logs in nested folders count toward their parent session's tokens.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import settings
from .usage import parse_time

SEARCH_TEXT_LIMIT = 20_000
COMMAND_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)


def short_model(model: str) -> str:
    return re.sub(r"-\d{8}$", "", model).replace("claude-", "")


def _user_text(content) -> str | None:
    """Text the person typed, or None for tool results and injected context."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        if not parts or any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(parts)
    else:
        return None
    text = text.strip()
    if not text:
        return None
    if text.startswith("<command-name>"):
        m = COMMAND_RE.search(text)
        return m.group(1).strip() if m else None
    if text.startswith("<") and not text.startswith("<pasted"):
        return None  # system reminders, local command output, task notifications
    return text


@dataclass
class Session:
    id: str
    path: Path
    project_dir: str
    cwd: str | None = None
    git_branch: str | None = None
    ai_title: str | None = None
    custom_title: str | None = None
    first_prompt: str | None = None
    last_prompt: str | None = None
    started: float | None = None
    updated: float | None = None
    prompts: int = 0
    replies: int = 0
    cost: float | None = None
    version: str | None = None
    models: dict = field(default_factory=lambda: defaultdict(int))
    tokens: dict = field(default_factory=lambda: {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0})
    search: list = field(default_factory=list)
    search_len: int = 0

    @property
    def title(self) -> str:
        t = self.custom_title or self.ai_title or self.first_prompt or "Untitled session"
        return t if len(t) <= 120 else t[:117] + "..."

    def summary(self) -> dict:
        return {
            "id": self.id, "title": self.title, "ai_title": self.ai_title, "first_prompt": (self.first_prompt or "")[:300],
            "last_prompt": (self.last_prompt or "")[:300], "cwd": self.cwd, "project": Path(self.cwd).name if self.cwd else self.project_dir,
            "git_branch": self.git_branch, "started": self.started, "updated": self.updated,
            "prompts": self.prompts, "replies": self.replies, "cost": self.cost, "version": self.version,
            "models": sorted(self.models, key=lambda m: -self.models[m]), "tokens": self.tokens,
        }


@dataclass
class _File:
    offset: int = 0
    mtime: float = 0
    size: int = 0
    partial: bytes = b""


class Index:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.files: dict[Path, _File] = {}
        # (message id, request id) -> (ts, model, in, out, cache_write, cache_read, project, session)
        self.token_records: dict[tuple, tuple] = {}
        self._lock = threading.Lock()
        self._last_scan = 0.0

    @property
    def root(self) -> Path:
        return settings.claude_dir / "projects"

    def refresh(self, max_age: float = 3.0) -> None:
        if time.time() - self._last_scan < max_age:
            return
        with self._lock:
            if time.time() - self._last_scan < max_age:
                return
            self._scan()
            self._last_scan = time.time()

    def _scan(self) -> None:
        if not self.root.exists():
            return
        seen = set()
        for path in self.root.rglob("*.jsonl"):
            rel = path.relative_to(self.root).parts
            if len(rel) < 2:
                continue
            seen.add(path)
            try:
                st = path.stat()
            except OSError:
                continue
            f = self.files.get(path)
            if f and (st.st_mtime, st.st_size) == (f.mtime, f.size):
                continue
            session_id = path.stem if len(rel) == 2 else rel[1]
            sess = self.sessions.get(session_id)
            if sess is None:
                if len(rel) != 2:
                    continue  # subagent log whose parent we have not seen yet; picked up next scan
                sess = self.sessions[session_id] = Session(session_id, path, rel[0])
            if f is None or st.st_size < f.offset:
                if f is not None and len(rel) == 2:  # file was rewritten; start this session over
                    sess = self.sessions[session_id] = Session(session_id, path, rel[0])
                f = self.files[path] = _File()
            self._read(path, f, sess, is_main=len(rel) == 2)
            f.mtime, f.size = st.st_mtime, st.st_size
        for gone in set(self.files) - seen:
            del self.files[gone]
            if gone.stem in self.sessions and self.sessions[gone.stem].path == gone:
                del self.sessions[gone.stem]

    def _read(self, path: Path, f: _File, sess: Session, is_main: bool) -> None:
        try:
            with path.open("rb") as fh:
                fh.seek(f.offset)
                chunk = fh.read()
        except OSError:
            return
        data = f.partial + chunk
        f.offset += len(chunk)
        lines = data.split(b"\n")
        f.partial = lines.pop()  # incomplete last line, finished on a later read
        for raw in lines:
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if isinstance(obj, dict):
                self._apply(sess, obj, is_main)

    def _apply(self, s: Session, o: dict, is_main: bool) -> None:
        t = o.get("type")
        ts = parse_time(o.get("timestamp"))
        if ts:
            ts = ts.timestamp()
            s.updated = max(s.updated or ts, ts)
            if is_main:
                s.started = min(s.started or ts, ts)
        if is_main:
            if o.get("cwd"):
                s.cwd = o["cwd"]
            if o.get("gitBranch"):
                s.git_branch = o["gitBranch"]
            if o.get("version"):
                s.version = o["version"]
        if t == "ai-title" and o.get("aiTitle"):
            s.ai_title = o["aiTitle"]
        elif t == "custom-title" and o.get("customTitle"):
            s.custom_title = o["customTitle"]
        elif t == "summary" and o.get("summary") and not s.ai_title:
            s.ai_title = o["summary"]
        elif t == "cost-state" and is_main and o.get("totalCostUSD") is not None:
            s.cost = float(o["totalCostUSD"])
        elif t == "user" and is_main and not o.get("isMeta") and not o.get("isSidechain"):
            text = _user_text((o.get("message") or {}).get("content"))
            if text:
                s.prompts += 1
                s.first_prompt = s.first_prompt or text
                s.last_prompt = text
                if s.search_len < SEARCH_TEXT_LIMIT:
                    s.search.append(text.lower()[:4000])
                    s.search_len += len(s.search[-1])
        elif t == "assistant":
            msg = o.get("message")
            if not isinstance(msg, dict):
                return
            if is_main and not o.get("isSidechain"):
                s.replies += 1
            usage, model = msg.get("usage"), msg.get("model") or "unknown"
            if isinstance(usage, dict) and not model.startswith("<") and ts:
                key = (msg.get("id"), o.get("requestId"))
                old = self.token_records.get(key)
                rec = (ts, model, usage.get("input_tokens") or 0, usage.get("output_tokens") or 0,
                       usage.get("cache_creation_input_tokens") or 0, usage.get("cache_read_input_tokens") or 0,
                       Path(o["cwd"]).name if o.get("cwd") else s.project_dir, s.id)
                # Streamed replies are logged several times; the last copy has the final counts.
                if old:
                    for k, i in (("input", 2), ("output", 3), ("cache_write", 4), ("cache_read", 5)):
                        s.tokens[k] -= old[i]
                    s.models[short_model(old[1])] -= old[2] + old[3]
                self.token_records[key] = rec
                for k, i in (("input", 2), ("output", 3), ("cache_write", 4), ("cache_read", 5)):
                    s.tokens[k] += rec[i]
                s.models[short_model(model)] += rec[2] + rec[3]

    # ---------- queries ----------

    def list(self) -> list[dict]:
        self.refresh()
        out = [s.summary() for s in self.sessions.values() if s.prompts or s.replies]
        out.sort(key=lambda s: -(s["updated"] or 0))
        return out

    def get(self, session_id: str) -> Session | None:
        self.refresh()
        return self.sessions.get(session_id)

    def search(self, query: str) -> set[str]:
        self.refresh()
        q = query.lower().strip()
        return {s.id for s in self.sessions.values()
                if q in s.title.lower() or (s.cwd and q in s.cwd.lower()) or any(q in t for t in s.search)}

    def stats(self, days: int = 14) -> dict:
        self.refresh()
        today = date.today()
        first = today - timedelta(days=days - 1)
        per_day = {first + timedelta(days=i): {"io": 0, "cache": 0, "messages": 0} for i in range(days)}
        tot = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "messages": 0}
        sessions_today, models_today = set(), defaultdict(int)
        projects = defaultdict(lambda: [0, 0])
        hours = [0] * 24
        last = None
        for ts, model, inp, out, cw, cr, project, sid in self.token_records.values():
            day = datetime.fromtimestamp(ts).date()
            if day < first:
                continue
            d = per_day.get(day)
            if d is None:
                continue
            d["io"] += inp + out
            d["cache"] += cw + cr
            d["messages"] += 1
            projects[project][0] += inp + out
            projects[project][1] += 1
            last = max(last or ts, ts)
            if day == today:
                for k, v in (("input", inp), ("output", out), ("cache_write", cw), ("cache_read", cr)):
                    tot[k] += v
                tot["messages"] += 1
                sessions_today.add(sid)
                models_today[short_model(model)] += inp + out
                hours[datetime.fromtimestamp(ts).hour] += inp + out
        tot["sessions"] = len(sessions_today)
        return {
            "today": tot,
            "today_models": sorted(models_today.items(), key=lambda kv: -kv[1]),
            "today_hours": hours,
            "days": [{"date": d.isoformat(), **v} for d, v in sorted(per_day.items())],
            "projects": [{"name": k, "tokens": v[0], "messages": v[1]}
                         for k, v in sorted(projects.items(), key=lambda kv: -kv[1][0])[:10]],
            "last_activity": last,
        }


def _tool_summary(name: str, inp: dict) -> str:
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt", "skill"):
        if isinstance(inp.get(key), str):
            return inp[key].splitlines()[0][:200] if inp[key] else ""
    return ""


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") if b.get("type") == "text" else f"[{b.get('type')}]"
                         for b in content if isinstance(b, dict))
    return ""


def transcript(sess: Session, limit: int = 400) -> list[dict]:
    """Conversation as a list of display items: user prompts, assistant text, and tool calls with results."""
    items: list[dict] = []
    tools: dict[str, dict] = {}
    seen_text: set[tuple] = set()
    try:
        fh = sess.path.open(encoding="utf-8", errors="replace")
    except OSError:
        return items
    with fh:
        for line in fh:
            if '"type":"user"' not in line and '"type":"assistant"' not in line and '"type": "user"' not in line \
                    and '"type": "assistant"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("isSidechain"):
                continue
            msg = o.get("message") or {}
            ts = o.get("timestamp")
            if o.get("type") == "user":
                content = msg.get("content")
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in tools:
                            text = _result_text(b.get("content"))
                            tool = tools[b["tool_use_id"]]
                            tool["result"] = text[:4000] + ("\n... (truncated)" if len(text) > 4000 else "")
                            tool["error"] = bool(b.get("is_error"))
                if o.get("isMeta"):
                    continue
                text = _user_text(content)
                if text:
                    items.append({"role": "user", "text": text, "ts": ts})
            else:
                for b in msg.get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text", "").strip():
                        key = (msg.get("id"), b["text"][:200])
                        if key in seen_text:
                            continue
                        seen_text.add(key)
                        items.append({"role": "assistant", "text": b["text"], "ts": ts, "model": msg.get("model")})
                    elif b.get("type") == "tool_use" and b.get("id") not in tools:
                        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                        tool = {"role": "tool", "name": b.get("name"), "summary": _tool_summary(b.get("name", ""), inp),
                                "input": json.dumps(inp, indent=2)[:4000], "result": None, "ts": ts}
                        tools[b["id"]] = tool
                        items.append(tool)
    return items[-limit:]


def live_claude_processes() -> dict[int, dict]:
    """Running Claude Code processes visible from here, from ~/.claude/sessions/<pid>.json."""
    out = {}
    folder = settings.claude_dir / "sessions"
    try:
        entries = list(folder.glob("*.json"))
    except OSError:
        return out
    for p in entries:
        try:
            info = json.loads(p.read_text(encoding="utf-8"))
            pid = int(info["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not _pid_matches(pid, info.get("procStart")):
            continue
        out[pid] = info
    return out


def _pid_matches(pid: int, proc_start) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    if not proc_start:
        return True
    # Field 22 is the start time; skip past the "(comm)" field, which can contain spaces.
    fields = stat[stat.rfind(")") + 2:].split()
    return len(fields) > 19 and fields[19] == str(proc_start)


index = Index()
