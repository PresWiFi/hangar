# Onboarding and providers: Phase 0 findings

Checked 2026-10-29 with Claude Code 2.1.308, Codex CLI 0.165.0, and Ollama 0.40.12 / 0.40.9.

## Claude login (verified)

- `claude auth login` works in a headless shell. It prints an authorize URL whose redirect is
  `https://platform.claude.com/oauth/code/callback`. That page shows a code, and the CLI waits
  at `Paste code here if prompted >`. Hangar can run this in a hidden tmux window, show the URL in
  the browser, and send the pasted code with `send-keys`. No API key or local callback port needed.
- `claude auth status --json` reports `loggedIn`, `authMethod`, `email`, `orgName`, and
  `subscriptionType`. Use it for the "Claude linked" check and the account card.
- `claude auth logout` exists for an "Unlink" button.
- `CLAUDE_CONFIG_DIR` keeps a login separate. Hangar-owned login = a config dir in Hangar's volume.
- Not yet run to completion (needs a person to sign in). Do that during Phase 2 testing.

## ChatGPT login through Codex (verified 2026-10-29)

- `codex login --device-auth` prints `https://auth.openai.com/codex/device` and a one-time code
  (`XXXX-XXXXX`, expires in 15 minutes), then prints `Successfully logged in` and exits 0.
  Easy to drive from tmux.
- **Device-code login must be turned on first** in ChatGPT Security Settings ("Enable device code
  sign-in for Codex, Excel, PowerPoint, and Word"). Until then the sign-in page shows that message.
  The same code worked after enabling it. Onboarding must say this up front, with a link.
- Fallback when device code is off: `codex login` listens on `localhost:1455` and redirects to
  `http://127.0.0.1:1455/auth/callback?code=…&state=…`. On a remote Hangar that page fails to load,
  but the user can paste the address-bar URL into Hangar, which requests it inside the container.
- Set `cli_auth_credentials_store = "file"` in `$CODEX_HOME/config.toml`; the login lands in
  `$CODEX_HOME/auth.json` (0600) with `auth_mode: "chatgpt"` and `tokens` (id, access, refresh,
  account_id). Hangar reads it, never writes it; Codex refreshes tokens itself.
- `codex login status` prints `Logged in using ChatGPT`, exit 0. `codex logout` prints `Successfully logged out`.

## Installing Codex (verified)

- Use the **full package**, not the bare binary: `codex-package-<arch>-unknown-linux-musl.tar.gz`
  (163 MB download, 426 MB unpacked: `bin/codex`, `bin/codex-code-mode-host`, `codex-path/rg`,
  `codex-resources/bwrap`, voice libs). The bare binary refuses to start interactive sessions in
  daemon mode, and with `--no-daemon` it fails every shell command (missing `codex-code-mode-host`).
- Checksums: the release asset `codex-package_SHA256SUMS` (verified for 0.165.0 x86_64). Hangar
  should pin the version and ship the expected sha256 per arch, not trust a downloaded sums file.
- Install on demand during onboarding into the home volume (e.g. `~/.local/share/hangar/codex/<version>`),
  so no image change, no sudo, and it survives container recreation.
- Pin a release at least two weeks old; 0.165.0 came out 2026-10-27.

## Codex sessions (verified 2026-10-29)

- Run with `--no-daemon` so each tmux pane owns its Codex process (no shared background server).
- No `--session-id` flag. The codex process keeps its rollout file open, so Hangar can link a tmux
  pane to a session through psutil `open_files()` on the pane's process tree, like the Claude pid match.
- Rollout path: `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<local time>-<uuid>.jsonl`. Lines are
  `{"timestamp", "type", "payload"}`. Seen in one turn: `session_meta`, `turn_context`, `world_state`,
  `response_item` (`message`, `additional_tools`), `event_msg` (`task_started`, `task_complete`,
  `item_completed`, `token_count`, `thread_settings_applied`), `token_usage_record`.
- Busy/idle: `event_msg` `task_started` / `task_complete`.
- `session_meta.payload` has `id`, `cwd`, `cli_version`, `model_provider`, `originator`, `timestamp`.
- Titles: `$CODEX_HOME/session_index.jsonl` has `{id, thread_name, updated_at}`.
- Transcript: `response_item` messages with role `user`/`assistant`. Skip role `developer` and user
  messages that start with `<environment_context>`.
- `codex resume --no-daemon <uuid> [prompt]` resumes with history and appends to the same rollout file.
- Rollouts older than 7 days are compressed to `.jsonl.zst`; read them with the image's `zstd` CLI
  or the `zstandard` package.
- Codex also keeps several SQLite files in `$CODEX_HOME` (state, logs, thread history). Hangar doesn't need them.

## Codex sandbox in Docker (verified: does not work)

- Codex sandboxes commands with bubblewrap, which needs unprivileged user namespaces. Docker's default
  profile blocks them (`unshare -Ur` fails here). With `-s workspace-write` every command fails:
  `bwrap: No permissions to create a new namespace`.
- So inside Hangar, Codex must run with `-s danger-full-access` (the container is the boundary, as it
  already is for Claude) and keep approvals on so the user still confirms commands. Loosening Docker's
  seccomp/AppArmor to allow namespaces was the alternative. Decided 2026-10-29: `-s danger-full-access` with approvals on (`-a on-request`); Docker stays unchanged.

## ChatGPT plan usage (verified)

- `event_msg` `token_count` carries `info` (token totals, `model_context_window`) and `rate_limits`:
  `{limit_id: "codex", primary: {used_percent, window_minutes, resets_at}, secondary, credits, plan_type}`.
  On a free plan: one 30-day window (43200 min), `secondary: null`. Build the usage card from the
  latest `token_count` across rollouts. It only updates while Codex is in use.

## Ollama (verified)

- Claude Code against Ollama works with `ANTHROPIC_BASE_URL=http://<host>:11434`,
  `ANTHROPIC_AUTH_TOKEN=ollama`, `ANTHROPIC_API_KEY=` and `--model <ollama model>`. These are
  per-session env vars, so they don't disturb the Claude login. Root URL, not `/v1`.
- Tested `claude -p` with `qwen3.5` (9.7B) on the LAN: correct reply. The first call took about
  70 s while the model loaded; later calls took about 1 s.
- Quirks: Claude Code prints `unrecognized_model`, reports a made-up `costUSD`, and assumes a
  200k context window. Hangar should hide cost for Ollama sessions. Ollama doesn't support prompt
  caching, token counting, or `tool_choice`.
- Model list: `GET /api/tags`. Health/version: `GET /api/version`.
- From the container, `host.docker.internal:11434` and a LAN address both answered, but with
  different Ollama versions and models, so they are two separate servers. Multiple Ollama servers
  should be allowed.

## Accounts and OIDC (verified locally)

- Password hashing: `hashlib.scrypt` is in Python 3.12, so no new dependency.
- OIDC: PyJWT (with `PyJWKClient`), cryptography, and httpx are already installed through `mcp`.
  Pin them directly in requirements.txt rather than relying on `mcp` to bring them in.
