# How Hangar works

Details behind the [README](../README.md).

## First run

| First-run setup | Connect your AI |
|---|---|
| ![Setup](setup.png) | ![Connect your AI](connect-ai.png) |

Hangar has one account, and only someone who can read its log (where the setup code is printed) can create it.

**Upgrading from a version before accounts:**

- Hangar creates the owner account `admin` with your `HANGAR_PASSWORD` (or the password it generated before). Sign in as `admin`, then rename the account in Settings if you like.
- Hangar now keeps its own Claude login by default instead of using the host's `~/.claude`. To keep sharing the host's login and history, add `CLAUDE_DIR=/home/you/.claude` to `.env` (the path the old default pointed at) before you run `docker compose up -d --build`.

## Agents and models

**New session** lets you pick the agent and model:

![New session](new-session.png)

- **Claude Code** with your Claude subscription (Opus, Sonnet, Haiku), or with any model on a linked Ollama server.
- **Codex** with your ChatGPT plan, or with an Ollama model (no ChatGPT sign-in needed for that).
- **Plain shell.**

Codex sessions get the same treatment as Claude's: they're listed in the sidebar (tagged *Codex*), have a transcript with tool calls, show working/waiting status, can be resumed, forked, and restored after a restart. The dashboard's token activity counts both, and a **ChatGPT usage** card shows your Codex plan limits (also in the sidebar, next to Claude's).

Sessions on Ollama are tagged *Ollama*. Hangar remembers which server and model a session used, so **Resume** and restore start it the same way. Claude Code's cost estimate is hidden for them, since it prices local models as if they were Claude.

## Connecting AI in detail

**Claude.** **Sign in with Claude** uses your Pro or Max subscription, like `claude /login`. The login is stored in Hangar's Claude folder (`CLAUDE_DIR`).

**ChatGPT (Codex).** Hangar runs OpenAI's [Codex CLI](https://github.com/openai/codex) for ChatGPT. It isn't in the image: the first time you pick ChatGPT, Hangar downloads a pinned Codex release from GitHub (about 163 MB), checks its SHA-256, and installs it in Hangar's data volume, so it survives rebuilds. Then you sign in with a device code:

1. In ChatGPT, open **Settings > Security** and turn on **device code sign-in** for Codex. You only do this once.
2. Open the link Hangar shows and enter the code.

If you can't use device codes, choose **Use browser sign-in instead**. After you sign in, your browser lands on a `localhost:1455` page that doesn't load. That's expected: copy that page's address and paste it into Hangar, which finishes the sign-in. The login is stored in `CODEX_HOME` (`~/.codex` in the container's home volume).

Codex's own command sandbox can't work inside Docker (it needs user namespaces). So when you start a Codex session you pick how it runs commands:

- **Ask before commands** (default): Codex keeps its sandbox. Each command fails there first, then Codex asks whether to run it for real. Safe, but a little slow, and small local models can retry a few times before asking.
- **Full access**: no sandbox and no questions. The container is the only boundary.

**Ollama.** Hangar finds Ollama on this machine or the Docker host by itself (`host.docker.internal:11434`); click **Add**. For another machine, enter its address, for example `192.168.1.20` or `http://gpu-box:11434`. That machine's Ollama must listen on the network (`OLLAMA_HOST=0.0.0.0`). Claude Code uses Ollama through its Anthropic-compatible API (Ollama 0.14 or later).

## GitHub in detail

Hangar runs the [GitHub CLI](https://cli.github.com/) (`gh auth login`, then `gh auth setup-git`), so git asks `gh` for a token whenever it talks to github.com. Clone, pull, and push work over HTTPS in every session, and Claude can use `gh` for pull requests and issues. If your git name and email aren't set yet, Hangar fills them in from your GitHub account (with GitHub's private noreply address when your email isn't public). The login lives in `~/.config/gh` in the container's home volume, so it survives rebuilds.

If you can't use the device code, choose **Use a token instead** and paste a classic personal access token with the `repo`, `read:org`, and `workflow` scopes. **Unlink** signs `gh` out and removes the git setting.

To start on a repo, open **New session**, pick the parent folder, open **Clone from GitHub**, and pick a repo (or type `owner/name`). Hangar clones it into that folder and selects it for the session.

## How sessions work

- **New session** opens a folder picker (limited to `WORKSPACE`). For Claude it starts `claude --session-id <uuid>` inside a new tmux session, and Hangar uses that ID to link the terminal to the conversation log.
- **Copying from the terminal:** drag over text and it's copied to the device you're on (tmux selects it and Hangar passes it to your clipboard). To use the browser's own selection instead, hold Shift while dragging, then press Ctrl+Shift+C (Cmd+C on a Mac). Programs that copy through the terminal, like Claude Code's `/copy`, work too. Hangar never lets the remote side read your clipboard.
- When the agent exits, the tmux session drops to a shell in the same folder, so nothing disappears unexpectedly. **Stop** closes the tmux session.
- Claude Code stores conversations in `~/.claude/projects/<folder-path>/<id>.jsonl`. Hangar reads those files incrementally and never changes them.
- Codex runs with `--no-daemon`, so each tmux session owns its Codex process. Codex can't be given an ID up front, so Hangar links the terminal to the conversation once the Codex process opens its log in `~/.codex/sessions/`. Codex compresses logs older than a week; Hangar reads those too (with `zstd`, which is in the image).
- The workspace is mounted at **the same path** inside the container. That's why `claude --resume` works for sessions started on the host, and the other way around.
- tmux runs inside the container. Stopping the container ends the tmux sessions but not the conversations. With `HANGAR_AUTORESTORE=true`, they're resumed automatically on the next start. Without it, the sidebar shows a **Restore** button.

## Tools inside the container

Claude runs inside the container, so its commands use what's installed there. The image starts with git, curl, ripgrep, jq, Python 3.12, and a few basics. There are three ways to get more:

- **Let Claude install it.** Claude can run `sudo apt-get install`. Every sudo command shows a permission prompt, even in auto mode, so nothing gets installed without your OK. Hangar passes Claude a short note saying it's in a Debian container and can do this.
- **Packages are kept.** Hangar records what's been installed with apt in `/data/apt-packages.txt` and reinstalls it in the background when the container is recreated (log: `/data/apt-restore.log`). User-level installs (`pip install --user`, `~/.local`, `~/.cargo`, `~/.nvm`) live in the home volume and are kept too. Anything else written to system folders, like `npm install -g`, is lost on recreate.
- **Bake it into the image.** Set `EXTRA_PACKAGES` in `.env`, for example `EXTRA_PACKAGES="build-essential golang nodejs npm"`, and rebuild.

To turn off sudo entirely, set `ALLOW_SUDO=false` in `.env` and rebuild. The sudo prompt is a Claude Code permission rule (`ask: Bash(sudo:*)`), so it doesn't apply in `bypassPermissions` mode.

If you need the host's full toolchain instead, run Hangar [without Docker](../README.md#without-docker). On native Linux, you can also try `docker-compose.host-tmux.yml`, which runs sessions in the host's tmux server.

## Reaching it over Tailscale

Turn on HTTPS certificates for your tailnet first (admin console > DNS). With the sidecar, also set `HANGAR_SECURE_COOKIES=true` if you only use HTTPS, and `HANGAR_BIND=127.0.0.1` so the plain port isn't open on your network.

## All settings

| Variable | Default | What it does |
|---|---|---|
| `WORKSPACE` (Docker) | | Projects folder, mounted at the same path inside the container. Sets `HANGAR_WORKSPACES`. |
| `HANGAR_BIND` (Docker) | `0.0.0.0` | Address the port is published on. |
| `EXTRA_PACKAGES` (build) | | Extra Debian packages baked into the image. |
| `HANGAR_PASSWORD` | empty | Optional. Skips the setup page and creates the owner account `admin` with this password. If you change it later, Hangar updates the password on its next start. Leave empty to use the setup page. |
| `HANGAR_WORKSPACES` | home folder | Comma-separated folders that new sessions can start in. |
| `HANGAR_PORT` / `HANGAR_HOST` | `7878` / `0.0.0.0` | Where the server listens. |
| `HANGAR_DATA_DIR` | `~/.local/share/hangar` (`/data` in Docker) | Hangar's database (including the owner account) and cookie secret. |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code's folder. In Docker, set `CLAUDE_DIR` in `.env` instead to choose what's mounted there (default: Hangar's own `hangar-claude` volume). |
| `CODEX_HOME` | `~/.codex` | Codex CLI's folder (login, config, session logs). |
| `HANGAR_CODEX_DOWNLOAD_BASE` | GitHub releases | Mirror or local folder (`file:///...`) to install Codex from. The SHA-256 check still applies. |
| `HANGAR_AUTORESTORE` | `false` (`true` in compose) | Resume the sessions that were running when Hangar last stopped. |
| `HANGAR_CLAUDE_ARGS` | | Extra arguments for every new or resumed session. |
| `ALLOW_SUDO` (build) | `true` | Docker only: give Claude sudo, with a permission prompt for each sudo command. |
| `HANGAR_CLAUDE_CMD` | `claude` | Path to the Claude Code binary. |
| `HANGAR_GH_CMD` | `gh` | Path to the GitHub CLI, used to link GitHub and clone repos. |
| `HANGAR_TMUX_SOCKET` | tmux default | Use a specific tmux server socket. |
| `HANGAR_USAGE_POLL_MINUTES` | `5` | How often to check plan usage (minimum 1; the endpoint rate-limits). |
| `HANGAR_MAX_UPLOAD_MB` | `512` | Largest file you can upload from the browser. |
| `HANGAR_SECURE_COOKIES` | `false` | Mark the login cookie HTTPS-only. |
| `HANGAR_PUBLIC_URL` | browser address | The address people open Hangar at, used for the SSO redirect URI. |
| `HANGAR_HOSTNAME` | system hostname | Name shown in the UI. |

## Limits

- The usage numbers come from the same unofficial endpoint Claude Code's `/usage` uses. It rate-limits fast polling, so Hangar checks every 5 minutes and backs off on errors. It only works with a Claude subscription login, not an API key.
- The "cost" on a session is Claude Code's API-equivalent estimate, not what a subscription charges.
- Live status (working, waiting) comes from the files Claude Code writes in `~/.claude/sessions`, matched to processes Hangar can see. For Codex it comes from the turn events in its log.
- ChatGPT usage is checked every 5 minutes with the same request Codex's `/status` makes, using the token Codex saved. Hangar never refreshes that token; if it has expired, the card shows the limits from Codex's last reply until you use Codex again. The endpoint is unofficial, like Claude's.
- Local models through Ollama are much weaker agents than Claude or GPT, and Claude Code features Ollama doesn't support (prompt caching, token counting) are skipped. The first message to a model that isn't loaded can take a minute.

## Code layout

The backend is FastAPI (`hangar/app.py`), with modules for tmux (`tmux.py`), Claude and Codex session logs (`sessions.py`, `codex_sessions.py`), usage (`usage.py`), the terminal bridge (`terminal.py`), the owner account (`auth.py`), SSO (`oidc.py`), and linking Claude, ChatGPT, and Ollama (`providers/`). The front end is plain JavaScript with no build step (`hangar/static/`). xterm.js, marked, and DOMPurify are vendored in `hangar/static/vendor`, so the UI works offline.
