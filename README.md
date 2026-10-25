# Hangar

**Park your Claude Code sessions. Pick them back up from any device.**

Hangar is a self-hosted web UI for [Claude Code](https://docs.claude.com/en/docs/claude-code/overview). Every session runs inside tmux, so it keeps going when you close the browser. You can open it again from your laptop, phone, or tablet, over your home network or Tailscale. The layout follows Open WebUI: your sessions are in a sidebar, grouped by date and titled automatically. Each one has a full terminal and a readable transcript.

It also has a dashboard with your Claude plan usage and token activity.

![Dashboard](docs/dashboard.png)

## Features

- **Session history.** Every Claude Code conversation on the machine, with its AI-generated title, project, git branch, token count, and cost. Search covers titles, folders, and everything you typed.
- **Live terminals in the browser.** Each session runs in its own tmux session. Attach from as many devices as you like. Closing the tab only detaches.
- **Resume anything.** Sessions that aren't running have a **Resume** button, which runs `claude --resume` in a new tmux session. **Fork** starts a copy with a new ID.
- **Survives restarts.** When the container comes back up, Hangar can reopen the sessions that were running.
- **Transcript view.** A readable version of the conversation, with Markdown rendered and tool calls folded.
- **Works on phones.** A key bar for Esc, Shift+Tab, arrows, Ctrl+C and so on, and a compose box for long messages. You can also add it to your home screen as an app.
- **Live status.** See which sessions are working, which are waiting for you, and which have dropped back to a shell.
- **Claude usage.** Your 5-hour and weekly limits (the same numbers `/usage` shows), when they reset, and a warning when you're on pace to hit 100%. Also shows Claude status page incidents.
- **Token activity.** Daily and hourly input and output tokens from the local logs, plus the busiest projects and the models used today.
- **Claude can install what it needs.** In Docker, Claude can `sudo apt-get install` missing tools. You get a permission prompt for every sudo command, even in auto mode, and installed packages come back after the container is recreated.
- **Pins, archive, renames, and private notes** for each session.

| Session terminal | Transcript | Phone |
|---|---|---|
| ![Terminal](docs/terminal.png) | ![Transcript](docs/transcript.png) | ![Phone](docs/phone.png) |

## Quick start (Docker)

You need Docker with Compose, and Claude Code signed in on the host (`claude`, then `/login`). Hangar reuses that login.

```bash
git clone https://github.com/PresWiFi/hangar.git
cd hangar
cp .env.example .env
# Edit .env: set WORKSPACE to your projects folder and HANGAR_PASSWORD to something long.
docker compose up -d --build
```

Open `http://<this-machine>:7878` and sign in.

To stop it, run `docker compose down`. Your conversations stay in `~/.claude` and can be resumed later. Run `docker compose up -d` to start it again.

To update Claude Code inside the image: `docker compose build --no-cache && docker compose up -d`.

### Windows (Docker Desktop + WSL)

Run Hangar from your WSL distro, not PowerShell, so it shares the same `~/.claude` and paths as Claude Code in WSL:

1. In Docker Desktop, open **Settings > Resources > WSL integration** and turn it on for your distro.
2. In WSL, set `WORKSPACE` to the path WSL uses, for example `/mnt/c/Users/you/code`.
3. Run `docker compose up -d --build` from WSL.

## Reaching it from other devices

**Home network:** with the default `HANGAR_BIND=0.0.0.0`, open `http://<machine-ip>:7878` from any device on the network. You may need to allow port 7878 in the host firewall.

**Tailscale, simple:** if the host is on your tailnet, `http://<machine-tailscale-ip>:7878` works with no extra setup.

**Tailscale with HTTPS and its own name:** run the sidecar. Hangar then shows up as its own device, for example `https://hangar.your-tailnet.ts.net`.

```bash
# In .env: TS_AUTHKEY=tskey-auth-...   (and HANGAR_SECURE_COOKIES=true if you only use HTTPS)
docker compose -f docker-compose.yml -f docker-compose.tailscale.yml up -d --build
```

Turn on HTTPS certificates for your tailnet first (admin console > DNS). If you only want to reach Hangar over Tailscale, set `HANGAR_BIND=127.0.0.1` so the plain port isn't open on your network.

## How sessions work

- **New session** opens a folder picker (limited to `WORKSPACE`) and starts `claude --session-id <uuid>` inside a new tmux session. Hangar uses that ID to link the terminal to the conversation log.
- When Claude exits, the tmux session drops to a shell in the same folder, so nothing disappears unexpectedly. **Stop** closes the tmux session.
- Claude Code stores conversations in `~/.claude/projects/<folder-path>/<id>.jsonl`. Hangar reads those files incrementally and never changes them.
- The workspace is mounted at **the same path** inside the container. That's why `claude --resume` works for sessions started on the host, and the other way around.
- tmux runs inside the container. Stopping the container ends the tmux sessions but not the conversations. With `HANGAR_AUTORESTORE=true`, they're resumed automatically on the next start. Without it, the sidebar shows a **Restore** button.

### Tools inside the container

Claude runs inside the container, so its commands use what's installed there. The image starts with git, curl, ripgrep, jq, Python 3.12, and a few basics. There are three ways to get more:

- **Let Claude install it.** Claude can run `sudo apt-get install`. Every sudo command shows a permission prompt, even in auto mode, so nothing gets installed without your OK. Hangar passes Claude a short note saying it's in a Debian container and can do this.
- **Packages are kept.** Hangar records what's been installed with apt in `/data/apt-packages.txt` and reinstalls it in the background when the container is recreated (log: `/data/apt-restore.log`). User-level installs (`pip install --user`, `~/.local`, `~/.cargo`, `~/.nvm`) live in the home volume and are kept too. Anything else written to system folders, like `npm install -g`, is lost on recreate.
- **Bake it into the image.** Set `EXTRA_PACKAGES` in `.env`, for example `EXTRA_PACKAGES="build-essential golang nodejs npm"`, and rebuild.

To turn off sudo entirely, set `ALLOW_SUDO=false` in `.env` and rebuild. The sudo prompt is a Claude Code permission rule (`ask: Bash(sudo:*)`), so it doesn't apply in `bypassPermissions` mode.

If you need the host's full toolchain instead, run Hangar without Docker (below). On native Linux, you can also try `docker-compose.host-tmux.yml`, which runs sessions in the host's tmux server.

## Running without Docker

Needs Linux or macOS (or WSL), Python 3.10+, tmux, and Claude Code.

```bash
python3 -m venv ~/.local/share/hangar-venv
~/.local/share/hangar-venv/bin/pip install -r requirements.txt
HANGAR_WORKSPACES=~/code HANGAR_PASSWORD='long-password' ~/.local/share/hangar-venv/bin/python -m hangar
```

Sessions then run in your own tmux server, with all your tools, and you can attach to them with `tmux attach -t hg-...`.

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `HANGAR_PASSWORD` | generated | Web UI password. If empty, Hangar generates one, saves it to `<data>/password`, and prints where it is at startup. |
| `HANGAR_WORKSPACES` | home folder | Comma-separated folders that new sessions can start in. |
| `HANGAR_PORT` / `HANGAR_HOST` | `7878` / `0.0.0.0` | Where the server listens. |
| `HANGAR_DATA_DIR` | `~/.local/share/hangar` (`/data` in Docker) | Hangar's database, cookie secret, and generated password. |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code's folder. |
| `HANGAR_AUTORESTORE` | `false` (`true` in compose) | Resume the sessions that were running when Hangar last stopped. |
| `HANGAR_CLAUDE_ARGS` | | Extra arguments for every new or resumed session. |
| `ALLOW_SUDO` (build) | `true` | Docker only: give Claude sudo, with a permission prompt for each sudo command. |
| `HANGAR_CLAUDE_CMD` | `claude` | Path to the Claude Code binary. |
| `HANGAR_TMUX_SOCKET` | tmux default | Use a specific tmux server socket. |
| `HANGAR_USAGE_POLL_MINUTES` | `5` | How often to check plan usage (minimum 1; the endpoint rate-limits). |
| `HANGAR_SECURE_COOKIES` | `false` | Mark the login cookie HTTPS-only. |
| `HANGAR_HOSTNAME` | system hostname | Name shown in the UI. |

## Security

Hangar gives whoever signs in a shell on your machine, with Claude Code and your Claude login. Treat the password like an SSH key.

- Use a long password. Logins are rate-limited (10 attempts per 5 minutes per address) and the cookie is signed. Changing the password signs everyone out.
- Don't forward the port to the internet. Use your LAN, Tailscale, or another VPN.
- Every API request needs the login cookie, and requests that change anything also need a custom header, which blocks cross-site requests. Terminal WebSockets check the page origin.
- New sessions can only start inside `HANGAR_WORKSPACES`. A terminal can still `cd` anywhere the container can see, so only mount what you need.
- In Docker, the container user has passwordless sudo inside the container (unless `ALLOW_SUDO=false`). That gives root in the container, not on the host, but root in the container can still change everything that's mounted.
- Hangar reads Claude Code's OAuth token to check usage. It never refreshes or changes it.

## Notes and limits

- The usage numbers come from the same unofficial endpoint Claude Code's `/usage` uses. It rate-limits fast polling, so Hangar checks every 5 minutes and backs off on errors. It only works with a Claude subscription login, not an API key.
- The "cost" on a session is Claude Code's API-equivalent estimate, not what a subscription charges.
- Live status (working, waiting) comes from the files Claude Code writes in `~/.claude/sessions`, matched to processes Hangar can see.

## Development

The backend is FastAPI (`hangar/app.py`), with modules for tmux (`tmux.py`), session logs (`sessions.py`), usage (`usage.py`), and the terminal bridge (`terminal.py`). The front end is plain JavaScript with no build step (`hangar/static/`). xterm.js, marked, and DOMPurify are vendored in `hangar/static/vendor`, so the UI works offline.

```bash
HANGAR_PASSWORD=dev HANGAR_PORT=7979 python -m hangar
```

## Credits

- [xterm.js](https://xtermjs.org/) (MIT), [marked](https://marked.js.org/) (MIT), [DOMPurify](https://github.com/cure53/DOMPurify) (Apache-2.0 / MPL-2.0)
- The usage dashboard is adapted from UsageMeter, a tray app by the same author.

Hangar is an independent project. It is not made by or affiliated with Anthropic. Claude and Claude Code are trademarks of Anthropic.

## License

MIT
