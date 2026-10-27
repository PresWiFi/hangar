# Ideas plugin: reconnaissance

Phase 0 notes for the optional Ideas plugin. This file maps the parts of Hangar the plugin touches and proposes the smallest plugin seam. No feature code exists yet.

Line numbers refer to the commit this branch starts from (`78e1898`).

## Summary

- **Database:** SQLite, through the standard library `sqlite3` module. One file, `hangar.db`, in `HANGAR_DATA_DIR`. There is no migration system: tables are created with `CREATE TABLE IF NOT EXISTS` when `hangar/store.py` is imported.
- **Routes:** every route is declared directly on the `FastAPI` app in `hangar/app.py`. No `APIRouter` is used yet.
- **Auth:** one HTTP middleware (`guard`) checks the session cookie on every `/api/*` path and requires `X-Hangar: 1` on every non-GET request. It runs before any router, so a token-authenticated plugin route needs a small change to `guard`.
- **Session launch:** `hangar/tmux.py` builds the `claude` command line in one place (`claude_command`). That is where plugin arguments go.
- **System prompt note:** exists only in the Docker image, through the `HANGAR_CLAUDE_BASE_ARGS` env var set in the `Dockerfile`. Native installs have no note.
- **Sidebar:** a session list. It has no navigation links to copy, so the plugin needs a small new nav slot.
- **Tests:** there are none. No test folder, no test dependencies, no CI.

## How things work today

### Routes and app startup (`hangar/app.py`)

| What | Where |
|---|---|
| Routes that skip the cookie check | `PUBLIC_API` (line 22) |
| Startup and shutdown work | `lifespan()` (lines 25-38): usage poller, session index, restore snapshot, autorestore, 15 s open-session tracker |
| App object | line 56; OpenAPI docs are turned off |
| Auth middleware | `guard()` (lines 63-77) |
| Info the UI loads at boot | `GET /api/me` (lines 111-116) |
| Static files | `app.mount("/static", ...)` (line 401), from `hangar/static/` |

Blocking work is pushed off the event loop with `asyncio.to_thread`. Errors are raised as `HTTPException` (helper `_err`, line 80). Request bodies are Pydantic models defined next to their route.

### Database (`hangar/store.py`)

- Engine: **SQLite** (`sqlite3`, standard library). File: `settings.data_dir / "hangar.db"`.
- One module-level connection opened with `check_same_thread=False`, plus one module-level `threading.Lock` that wraps every query.
- `row_factory = sqlite3.Row`; rows are returned as dicts.
- Tables: `kv` (JSON values by key) and `meta` (per-session name, pinned, archived, notes).
- Schema is created at import with `executescript("CREATE TABLE IF NOT EXISTS ...")`. No version table, no migrations.
- The connection opens at import time, and `settings` is read from env at import time (`hangar/config.py`, `settings = Settings.from_env()`). Tests must set env vars (especially `HANGAR_DATA_DIR`) before importing any `hangar` module.

### Auth (`hangar/auth.py`, `hangar/app.py`)

- Password from `HANGAR_PASSWORD`, or generated into `<data dir>/password`.
- `_read_or_create(name, make)` (auth.py lines 18-25) writes a secret file once with mode `0600`. A plugin token can reuse this pattern.
- Cookie `hangar_session`: `"<expiry>.<nonce>.<hmac>"`, keyed on the stored secret plus the password, so changing the password signs everyone out. HttpOnly, SameSite=Lax, 30 days.
- `guard()`:
  1. For `/api/*` paths not in `PUBLIC_API`: no valid cookie means 401.
  2. For any method other than GET and HEAD: no `X-Hangar: 1` header means 403. Browsers can't add a custom header cross-site without a CORS preflight, so this blocks CSRF.
  3. Adds `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and `Cache-Control: no-cache` for HTML, JS, and CSS.
- WebSockets: `/ws/term/{name}` checks the cookie and that `Origin` matches `Host` (`_same_origin`, line 367).
- Login is rate limited to 10 attempts per 5 minutes per IP.

### Session launch (`hangar/tmux.py`)

| What | Where |
|---|---|
| Full `claude` command line | `claude_command(extra)` (lines 142-147): `settings.claude_base_args` + `settings.claude_args` (`HANGAR_CLAUDE_ARGS`) + `extra`, each shell-quoted, then a fallback shell |
| `--session-id <new uuid>` for new sessions | `new_session()` line 161 |
| `--resume <id>` and optional `--fork-session` | `new_session()` line 158 |
| Env passed to the tmux session | line 174: only `TERM` and `COLORTERM` |
| Link between tmux and Claude | tmux user options `@hangar_claude` and `@hangar_label` (lines 177-179) |
| Restore after restart | `restore()` (line 219) calls `new_session(..., resume=...)` |

New, resumed, forked, and restored sessions all go through `claude_command`, so one change there covers every launch path.

The system prompt note: the `Dockerfile` sets
`HANGAR_CLAUDE_BASE_ARGS="--settings /etc/hangar/claude-settings.json --append-system-prompt-file /etc/hangar/container-prompt.md"` (line 49), copied from `docker/`. `config.py` reads it into `settings.claude_base_args`. Native installs leave it empty.

### Front end (`hangar/static/`)

- `index.html`: the sidebar is `<aside id="sidebar">` (lines 32-47): brand link, "New session" button, search box, filters, `<nav id="session-list">`, footer. Scripts are plain `<script>` tags (lines 56-61); `app.js` is loaded last.
- `app.js` is one file with no modules:
  - `api(path, opts)` (line 21) always sends `X-Hangar: 1` and handles 401 by showing the login form.
  - `esc()` (line 5) escapes HTML. Markdown is rendered with `DOMPurify.sanitize(marked.parse(...))` (line 804).
  - `renderSidebar()` (line 211) rebuilds the session list with `innerHTML`.
  - Hash router `route()` (line 381): `#/s/<id>`, `#/t/<tmux name>`, otherwise the dashboard. A view is a function that returns an object with `dispose()`.
  - `boot()` (line 991) loads `/api/me`, then starts polling and routing.
- Mobile: the sidebar becomes a drawer (`toggleSide`, `#scrim`), and each view has a `topbar()` with a menu button.

### Tests

None. `requirements.txt` lists only `fastapi`, `uvicorn[standard]`, and `psutil`. FastAPI's `TestClient` needs `httpx`, and `pytest` is not installed. Phase 1 has to set the test conventions.

## Claude Code and MCP, verified against current docs

Checked on the current Claude Code docs and the `mcp` package on PyPI. The runbook named these from memory; the results below replace those guesses.

| Topic | Finding |
|---|---|
| MCP config flag | `--mcp-config <files or JSON strings...>`, space separated. Works in interactive sessions. `--strict-mcp-config` would ignore the user's other servers, so don't use it. |
| Config format | `{"mcpServers": {"<name>": {"command": "...", "args": [...], "env": {...}}}}`. An entry with no `type` is stdio. `${VAR}` and `${VAR:-default}` expand in `command`, `args`, `env`, `url`, `headers`. |
| Env inherited by a stdio server | The user's environment, plus the config's `env`. If the user sets `CLAUDE_CODE_MCP_ALLOWLIST_ENV=1`, the server gets only a safe baseline plus `env`, so anything the shim needs must be in `env`. |
| Session ID in the server | **`CLAUDE_CODE_SESSION_ID` is set for stdio MCP servers.** It holds the ID the server was spawned with. With `--resume <id>` it's the resumed ID. It does not change on `/clear`. |
| Appending to the system prompt | `--append-system-prompt "<text>"` and `--append-system-prompt-file <path>`. From v2.1.283 the inline and file forms can be combined. Passing the same flag twice isn't documented. |
| Resumed conversations | The system prompt is recorded on the conversation's first request and reused on `--resume` until compaction. A resumed conversation that predates the plugin keeps its old prompt, but it does get the MCP tools, because `--mcp-config` is passed again. |
| Tool permissions | MCP tools prompt by default. Allow rule: `mcp__<server>__*` (or `mcp__<server>`), through `--allowedTools` or a settings file. |
| Sessions outside Hangar | `claude mcp add [--scope user] [-e KEY=value ...] --transport stdio <name> -- <command> [args...]`. Put another option between `-e` and the name. Or use `claude mcp add-json <name> '<json>'`. |
| Timeouts and limits | `MCP_TIMEOUT` (startup, default 30 s). Output over 10k tokens shows a warning; the limit is 25k (`MAX_MCP_OUTPUT_TOKENS`). |
| Python MCP SDK | `mcp` 2.3.0 (2026-10-02), Python 3.10 or later. **v2 renamed `FastMCP` to `MCPServer`:** `from mcp.server.mcpserver import MCPServer`, `@server.tool()`, `server.run()` (stdio is the default). Errors: `mcp.server.mcpserver.exceptions.ToolError`. It resolves cleanly with Hangar's pinned `fastapi==0.115.6` (starlette 0.41.3), but adds about 30 packages, including `cryptography` and `python-multipart`. |

## Files and functions to touch

### Core files (kept small)

| File | Change |
|---|---|
| `hangar/config.py` | `Settings.plugins: list[str]` from `HANGAR_PLUGINS` (comma-separated, e.g. `HANGAR_PLUGINS=ideas`). This is the only on/off switch. |
| `hangar/app.py` | Load plugins after `app` is created. `include_router` for each one. In `lifespan`, run each plugin's startup hook. In `guard`, let a plugin authorize requests under its own prefix (bearer token) before the cookie check, and skip the `X-Hangar` check for those requests only. In `/api/me`, add `plugins: [{name, nav, script}]`. |
| `hangar/tmux.py` | `claude_command()`: add the args from enabled plugins after `claude_args` |
| `hangar/static/index.html` | Empty `<div id="side-nav">` under the "New session" button |
| `hangar/static/app.js` | A small plugin registry (`window.hangar = {api, esc, toast, topbar, setMain, md, registerRoute}`). `route()` checks registered routes first. `boot()` loads plugin scripts from `me.plugins` and draws nav links before the first `route()`. |
| `requirements.txt` | Add `mcp==2.3.0`. Only `hangar/ideas/mcp.py` imports it, so Hangar's core never loads it. |
| `requirements-dev.txt` (new) | `pytest`, `httpx` |
| `README.md` | Env var table rows and a short Ideas section (Phase 4) |

### New files

```
hangar/plugins/__init__.py     Plugin dataclass, load(), helpers used by app.py and tmux.py
hangar/ideas/__init__.py       PLUGIN = Plugin(...)
hangar/ideas/db.py             <data dir>/ideas.db: own connection and lock, migrations, ideas_schema_version
hangar/ideas/repo.py           queries, dedupe
hangar/ideas/api.py            APIRouter(prefix="/api/ideas")
hangar/ideas/mcp.py            stdio shim: python -m hangar.ideas.mcp
hangar/ideas/prompt.md         standing instruction
hangar/static/plugins/ideas.js UI (plain JS)
hangar/static/plugins/ideas.css
tests/conftest.py              sets HANGAR_DATA_DIR to a temp folder before importing hangar
tests/test_ideas_*.py
```

## Decisions taken after review

- **Storage:** a separate SQLite file, `<data dir>/ideas.db`, with its own connection and lock. Tables keep the `ideas_` prefix, and the schema version is tracked in `ideas_schema_version`. Hangar's core `hangar.db` is untouched, and turning the plugin off leaves nothing behind in it.
- **MCP library:** `mcp==2.3.0` pinned in `requirements.txt`.
- **Switch:** `HANGAR_PLUGINS=ideas`. There is no separate `HANGAR_IDEAS` variable.

## Proposed plugin seam

Only what Ideas uses:

```python
@dataclass
class Plugin:
    name: str                                   # "ideas"
    router: APIRouter                           # prefix /api/<name>
    nav: dict | None = None                     # {"label": "Ideas", "href": "#/ideas"}
    script: str | None = None                   # "/static/plugins/ideas.js"
    startup: Callable[[], None] | None = None   # run migrations, create the token
    claude_args: Callable[[], list[str]] | None = None   # extra args for every session
    authorize: Callable[[Request], bool] | None = None   # bearer check for its own prefix
```

- `load(names)` imports `hangar.<name>` and reads its `PLUGIN`. An unknown name logs a warning and is skipped.
- With no plugins enabled, nothing is imported, no routes exist, `/api/me` returns `plugins: []`, and the UI draws no nav. That matches "entirely absent when disabled".
- `authorize` is only consulted for paths under `/api/<name>/`. It returns True only for a valid bearer token. Anything else falls through to the normal cookie and `X-Hangar` checks. No new unauthenticated routes.

### Ideas launch arguments

`claude_args()` for Ideas returns:

```
--mcp-config <data dir>/ideas-mcp.json
--allowedTools mcp__hangar-ideas__*
--append-system-prompt <contents of prompt.md>
```

- `ideas-mcp.json` (mode 0600, rewritten at startup):
  `{"mcpServers": {"hangar-ideas": {"command": "<sys.executable>", "args": ["-m", "hangar.ideas.mcp"], "env": {"HANGAR_URL": "http://127.0.0.1:<port>", "HANGAR_IDEAS_TOKEN_FILE": "<data dir>/ideas-token"}}}}`.
  `sys.executable` makes it work from a native venv as well as in Docker. The token stays out of the command line, so it doesn't show up in `ps` or tmux.
- The shim reads `CLAUDE_CODE_SESSION_ID` from its environment and sends it with every call. Hangar doesn't need to pass any extra env var.
- The inline `--append-system-prompt` combines with the image's `--append-system-prompt-file`. That combination is documented; passing two file flags is not.

## Known limits and edge cases

- **`/clear` inside a session:** the MCP server keeps the session ID it started with, so after `/clear` new items link to the earlier conversation ID. It's acceptable for v1 and gets documented.
- **`--fork-session`:** which ID the server gets isn't documented. Hangar itself records the parent ID for a fork until the 15 s tracker sees the new one. Test this in Phase 3.
- **Resumed old conversations** keep their recorded system prompt. They get the tools, but not the standing instruction, so tool descriptions must carry the key rules too (search before create, done criteria).
- **`docker-compose.host-tmux.yml`:** sessions run on the host's tmux server, where the image's Python and data dir don't exist. The existing container note already has this problem. Ideas will be unsupported in that mode at first, and the docs will say so.
- **`HANGAR_HOST` bound to one address:** the shim URL should use that address instead of `127.0.0.1` when the host isn't `0.0.0.0`.
- **Variadic flags:** `--mcp-config` and `--allowedTools` take several values. If one is the last flag before a positional prompt (Phase 2's "start session with the next milestone" may pass one), it will swallow the prompt. Put plugin args before `extra`, and pass any opening prompt after `--`, or send it with `tmux.send_text` once Claude is ready.
- `.dockerignore` excludes `docs/`, so these notes are not shipped in the image.
