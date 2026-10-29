"""Index of Codex CLI sessions, from the rollout logs in $CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl.

Each line is {"timestamp", "type", "payload"}. Codex compresses rollouts older than a week to .jsonl.zst;
those don't change any more, so they're decompressed once with the zstd tool.
Sessions come out in the same shape as Claude's (sessions.Session) with agent="codex".
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import settings
from .sessions import SEARCH_TEXT_LIMIT, Session
from .usage import parse_time

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass
class CodexSession(Session):
    task_open: bool = False      # a turn started and hasn't finished (busy)
    last_total: int = 0          # cumulative tokens at the last token_count, to skip repeats
    model: str | None = None     # model of the latest turn


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("text"))
    return ""


def _prompt(text: str) -> str | None:
    """Text the person typed, or None for context Codex injects (environment, AGENTS.md, ...)."""
    text = (text or "").strip()
    if not text or text.startswith("<") or text.startswith("# AGENTS.md instructions"):
        return None
    return text


@dataclass
class _File:
    offset: int = 0
    mtime: float = 0
    size: int = 0
    partial: bytes = b""
    session: str | None = None


class CodexIndex:
    def __init__(self) -> None:
        self.sessions: dict[str, CodexSession] = {}
        self.files: dict[Path, _File] = {}
        self.records: dict[tuple, tuple] = {}       # (sid, total) -> (ts, model, in, out, 0, cache_read, project, sid)
        self.rate_limits: tuple[float, dict] | None = None
        self.titles: dict[str, str] = {}
        self._titles_mtime = 0.0
        self._lock = threading.Lock()
        self._last_scan = 0.0

    @property
    def root(self) -> Path:
        return settings.codex_home / "sessions"

    def refresh(self, max_age: float = 3.0) -> None:
        if time.time() - self._last_scan < max_age:
            return
        with self._lock:
            if time.time() - self._last_scan < max_age:
                return
            self._scan()
            self._last_scan = time.time()

    def _scan(self) -> None:
        self._read_titles()
        if not self.root.exists():
            return
        seen = set()
        for path in self.root.rglob("rollout-*.jsonl*"):
            if not (path.name.endswith(".jsonl") or path.name.endswith(".jsonl.zst")):
                continue
            seen.add(path)
            try:
                st = path.stat()
            except OSError:
                continue
            f = self.files.get(path)
            if f and (st.st_mtime, st.st_size) == (f.mtime, f.size):
                continue
            if path.name.endswith(".zst"):
                plain = path.with_name(path.name[:-4])
                if plain in self.files:  # just compressed; we already read it
                    self.files[path] = self.files.pop(plain)
                    self.files[path].mtime, self.files[path].size = st.st_mtime, st.st_size
                    continue
                f = self.files[path] = _File()
                self._read_compressed(path, f)
            else:
                if f is None or st.st_size < f.offset:
                    f = self.files[path] = _File()
                self._read(path, f)
            f.mtime, f.size = st.st_mtime, st.st_size
        for gone in set(self.files) - seen:
            f = self.files.pop(gone)
            if f.session and not any(o.session == f.session for o in self.files.values()):
                self.sessions.pop(f.session, None)

    def _read_titles(self) -> None:
        path = settings.codex_home / "session_index.jsonl"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if mtime == self._titles_mtime:
            return
        self._titles_mtime = mtime
        titles = {}
        for line in path.read_text(errors="replace").splitlines():
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if isinstance(o, dict) and o.get("id") and o.get("thread_name"):
                titles[o["id"]] = o["thread_name"]
        self.titles = titles

    def _read(self, path: Path, f: _File) -> None:
        try:
            with path.open("rb") as fh:
                fh.seek(f.offset)
                chunk = fh.read()
        except OSError:
            return
        data = f.partial + chunk
        f.offset += len(chunk)
        lines = data.split(b"\n")
        f.partial = lines.pop()
        self._lines(path, f, lines)

    def _read_compressed(self, path: Path, f: _File) -> None:
        if not shutil.which("zstd"):
            return
        try:
            data = subprocess.run(["zstd", "-dcq", str(path)], capture_output=True, timeout=60).stdout
        except (OSError, subprocess.TimeoutExpired):
            return
        self._lines(path, f, data.split(b"\n"))

    def _lines(self, path: Path, f: _File, lines: list[bytes]) -> None:
        for raw in lines:
            if not raw.strip():
                continue
            try:
                o = json.loads(raw)
            except ValueError:
                continue
            if isinstance(o, dict):
                self._apply(path, f, o)

    def _session_for(self, path: Path, f: _File) -> CodexSession | None:
        if f.session:
            return self.sessions.get(f.session)
        m = UUID_RE.search(path.name)
        if not m:
            return None
        f.session = m.group(0)
        return self.sessions.setdefault(f.session, CodexSession(f.session, path, "", agent="codex"))

    def _apply(self, path: Path, f: _File, o: dict) -> None:
        t, p = o.get("type"), o.get("payload")
        if not isinstance(p, dict):
            return
        if t == "session_meta" and p.get("id") and not f.session:
            f.session = p["id"]
            self.sessions.setdefault(f.session, CodexSession(f.session, path, "", agent="codex"))
        s = self._session_for(path, f)
        if s is None:
            return
        ts = parse_time(o.get("timestamp"))
        ts = ts.timestamp() if ts else None
        if ts:
            s.updated = max(s.updated or ts, ts)
            s.started = min(s.started or ts, ts)
        if t == "session_meta":
            s.path = path
            if p.get("cwd"):
                s.cwd = p["cwd"]
                s.project_dir = Path(p["cwd"]).name
            s.version = p.get("cli_version") or s.version
            git = p.get("git") if isinstance(p.get("git"), dict) else {}
            s.git_branch = git.get("branch") or s.git_branch
        elif t == "turn_context":
            s.model = p.get("model") or s.model
            if p.get("cwd") and not s.cwd:
                s.cwd = p["cwd"]
        elif t == "response_item" and p.get("type") == "message":
            if p.get("role") == "user":
                text = _prompt(_text(p.get("content")))
                if text:
                    s.prompts += 1
                    s.first_prompt = s.first_prompt or text
                    s.last_prompt = text
                    if s.search_len < SEARCH_TEXT_LIMIT:
                        s.search.append(text.lower()[:4000])
                        s.search_len += len(s.search[-1])
            elif p.get("role") == "assistant":
                s.replies += 1
        elif t == "event_msg":
            kind = p.get("type")
            if kind == "task_started":
                s.task_open = True
            elif kind == "task_complete":
                s.task_open = False
            elif kind == "token_count":
                self._tokens(s, p, ts)

    def _tokens(self, s: CodexSession, p: dict, ts: float | None) -> None:
        info = p.get("info") if isinstance(p.get("info"), dict) else {}
        total = (info.get("total_token_usage") or {}).get("total_tokens") or 0
        last = info.get("last_token_usage") or {}
        if total and total > s.last_total and ts:
            s.last_total = total
            cached = last.get("cached_input_tokens") or 0
            inp = max(0, (last.get("input_tokens") or 0) - cached)
            out = last.get("output_tokens") or 0
            model = s.model or "codex"
            s.tokens["input"] += inp
            s.tokens["output"] += out
            s.tokens["cache_read"] += cached
            s.models[model] += inp + out
            self.records[(s.id, total)] = (ts, model, inp, out, 0, cached, s.project_dir or "codex", s.id)
        rl = p.get("rate_limits")
        if isinstance(rl, dict) and (rl.get("primary") or rl.get("secondary")) and ts:
            if not self.rate_limits or ts >= self.rate_limits[0]:
                self.rate_limits = (ts, rl)

    # ---------- queries (same shape as sessions.Index) ----------

    def _titled(self, s: CodexSession) -> CodexSession:
        s.ai_title = self.titles.get(s.id) or s.ai_title
        return s

    def list(self) -> list[dict]:
        self.refresh()
        return [self._titled(s).summary() for s in self.sessions.values() if s.prompts or s.replies]

    def get(self, session_id: str) -> CodexSession | None:
        self.refresh()
        s = self.sessions.get(session_id)
        return self._titled(s) if s else None

    def search(self, query: str) -> set[str]:
        self.refresh()
        q = query.lower().strip()
        return {s.id for s in self.sessions.values()
                if q in self._titled(s).title.lower() or (s.cwd and q in s.cwd.lower()) or any(q in t for t in s.search)}

    def token_records(self) -> list[tuple]:
        self.refresh()
        return list(self.records.values())

    def usage(self) -> dict | None:
        """ChatGPT plan limits from the newest Codex turn, or None if Codex hasn't reported any."""
        self.refresh()
        if not self.rate_limits:
            return None
        ts, rl = self.rate_limits
        windows = []
        for key in ("primary", "secondary"):
            w = rl.get(key)
            if isinstance(w, dict) and w.get("used_percent") is not None:
                windows.append({"key": key, "used_percent": w["used_percent"], "window_minutes": w.get("window_minutes"),
                                "resets_at": w.get("resets_at")})
        return {"as_of": ts, "plan": rl.get("plan_type"), "windows": windows, "credits": rl.get("credits")}

    def session_for_file(self, path: str) -> str | None:
        m = UUID_RE.search(Path(path).name)
        return m.group(0) if m else None


def _items_from(path: Path) -> list[dict]:
    if path.name.endswith(".zst"):
        try:
            data = subprocess.run(["zstd", "-dcq", str(path)], capture_output=True, timeout=60).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        raw_lines = data.decode("utf-8", "replace").splitlines()
    else:
        try:
            raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
    out = []
    for line in raw_lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if isinstance(o, dict):
            out.append(o)
    return out


def _call_summary(name: str, args: dict) -> str:
    for key in ("cmd", "command", "path", "file_path", "query", "url", "pattern"):
        v = args.get(key)
        if isinstance(v, list):
            v = " ".join(map(str, v))
        if isinstance(v, str) and v:
            return v.splitlines()[0][:200]
    return ""


def _call_output(text: str) -> str:
    """exec_command results start with a header (chunk id, wall time, ...); keep what follows "Output:"."""
    if "\nOutput:\n" in text:
        head, _, body = text.partition("\nOutput:\n")
        code = re.search(r"exited with code (\d+)", head)
        return body if not code or code.group(1) == "0" else f"[exit {code.group(1)}]\n{body}"
    return text


def transcript(sess: CodexSession, limit: int = 400) -> list[dict]:
    """Same display items as sessions.transcript: user, assistant, and tool calls with results."""
    items: list[dict] = []
    calls: dict[str, dict] = {}
    for o in _items_from(sess.path):
        if o.get("type") != "response_item" or not isinstance(o.get("payload"), dict):
            continue
        p, ts = o["payload"], o.get("timestamp")
        kind = p.get("type")
        if kind == "message":
            text = _text(p.get("content"))
            if p.get("role") == "user":
                text = _prompt(text)
                if text:
                    items.append({"role": "user", "text": text, "ts": ts})
            elif p.get("role") == "assistant" and text.strip():
                items.append({"role": "assistant", "text": text, "ts": ts, "model": sess.model})
        elif kind in ("function_call", "custom_tool_call", "local_shell_call"):
            raw = p.get("arguments") if kind == "function_call" else p.get("input") or p.get("action")
            try:
                args = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except ValueError:
                args = {"input": raw}
            if not isinstance(args, dict):
                args = {"input": args}
            call = {"role": "tool", "name": p.get("name") or kind, "summary": _call_summary(p.get("name", ""), args),
                    "input": json.dumps(args, indent=2)[:4000], "result": None, "ts": ts}
            if p.get("call_id"):
                calls[p["call_id"]] = call
            items.append(call)
        elif kind in ("function_call_output", "custom_tool_call_output") and p.get("call_id") in calls:
            out = p.get("output")
            if isinstance(out, dict):
                out = out.get("content") or json.dumps(out)
            text = _call_output(str(out or ""))
            call = calls[p["call_id"]]
            call["result"] = text[:4000] + ("\n... (truncated)" if len(text) > 4000 else "")
            call["error"] = text.startswith("[exit ")
    return items[-limit:]


codex_index = CodexIndex()
