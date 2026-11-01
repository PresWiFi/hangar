# Hangar

**Park your coding agent sessions. Pick them back up from any device.**

Hangar is a self-hosted web UI for [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) and OpenAI's [Codex CLI](https://github.com/openai/codex). Every session runs in tmux, so it keeps going when you close the browser, and you can reopen it from your laptop, phone, or tablet. It works with your Claude or ChatGPT plan (no API keys) or local models through Ollama or llama.cpp.

![Dashboard](docs/dashboard.png)

## Features

- **Sessions in a sidebar**, titled automatically and searchable, each with a live terminal and a readable transcript.
- **Resume or fork** any past Claude Code or Codex conversation. Running sessions come back after a restart.
- **Usage dashboard**: your Claude and ChatGPT plan limits, when they reset, and daily token activity.
- **Works on phones**, with a key bar (Esc, Shift+Tab, arrows, Ctrl+C) and a compose box.
- **GitHub without SSH keys**: sign in once, then clone, pull, and push from any session.
- **File uploads and downloads** for each session's folder.
- **One owner account**, with optional SSO (Authentik, Microsoft, Google, Keycloak, Duo, or any OpenID Connect provider).

| Session terminal | Transcript | Phone |
|---|---|---|
| ![Terminal](docs/terminal.png) | ![Transcript](docs/transcript.png) | ![Phone](docs/phone.png) |

## Install

### Docker

```bash
git clone https://github.com/PresWiFi/hangar.git && cd hangar
cp .env.example .env            # set WORKSPACE to your projects folder
docker compose up -d --build
docker compose logs hangar      # shows the one-time setup code
```

Open `http://<this-machine>:7878`, enter the setup code, and create your account.

On **Windows**, run these commands in WSL (not PowerShell), with Docker Desktop's WSL integration turned on for your distro, and use a WSL path for `WORKSPACE`, such as `/mnt/c/Users/you/code`.

### Proxmox VE

Run this in the Proxmox host's shell:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/PresWiFi/hangar/main/proxmox/ct/hangar.sh)"
```

It creates a Debian 13 LXC container with Hangar, Claude Code, and the GitHub CLI installed directly (no Docker). Open `http://<container-ip>:7878` and get the setup code with `journalctl -u hangar`. Run `update` in the container to upgrade. Projects go in `/root/claude`.

### Without Docker

Needs Linux, macOS, or WSL, with Python 3.10+, tmux, and Claude Code (plus `gh` for GitHub and `zstd` for Codex).

```bash
python3 -m venv ~/.local/share/hangar-venv
~/.local/share/hangar-venv/bin/pip install -r requirements.txt
HANGAR_WORKSPACES=~/code ~/.local/share/hangar-venv/bin/python -m hangar   # prints the setup code
```

Sessions then run in your own tmux server, with all your tools.

## Connect your accounts

Hangar asks after setup; you can also do it later in **Settings**.

- **Claude**: click **Sign in with Claude** and paste back the code claude.ai shows. Uses your Pro or Max plan.
- **ChatGPT**: Hangar downloads Codex for you, then shows a device code to enter. First turn on **device code sign-in** in ChatGPT under **Settings > Security**.
- **Local models**: Ollama and llama.cpp servers on this machine are found automatically. For another machine, pick the type and enter its address. Ollama must listen with `OLLAMA_HOST=0.0.0.0`; start `llama-server` with `--host 0.0.0.0` and at least 32k context (`-c 65536`).
- **GitHub**: click **Sign in with GitHub** and enter the code on GitHub, or paste a token with the `repo`, `read:org`, and `workflow` scopes. Then use **New session > Clone from GitHub** to clone a repo.

## Reach it from other devices

- **Home network**: `http://<machine-ip>:7878`.
- **Tailscale**: `http://<tailscale-ip>:7878` if the machine is on your tailnet. For HTTPS and its own name (`https://hangar.<tailnet>.ts.net`), set `TS_AUTHKEY` in `.env` and run:
  ```bash
  docker compose -f docker-compose.yml -f docker-compose.tailscale.yml up -d --build
  ```

Don't expose port 7878 to the internet: Hangar gives whoever signs in a shell on your machine.

## Everyday commands

| Task | Command |
|---|---|
| Stop / start | `docker compose down` / `docker compose up -d` |
| Update Claude Code in the image | `docker compose build --no-cache && docker compose up -d` |
| Reset the password | `docker exec -it hangar python -m hangar reset-password` |

Conversations are kept when Hangar stops and can be resumed later.

## Configuration

Set these in `.env`. The common ones:

| Variable | Default | What it does |
|---|---|---|
| `WORKSPACE` | | Your projects folder. New sessions start inside it. |
| `HANGAR_PASSWORD` | empty | Skip the setup page and create the account `admin` with this password. |
| `CLAUDE_DIR` | Hangar's own volume | Mount your host's `~/.claude` to share its login and history. |
| `HANGAR_BIND` | `0.0.0.0` | `127.0.0.1` keeps the port off your network (e.g. Tailscale only). |
| `HANGAR_AUTORESTORE` | `true` | Reopen the sessions that were running when Hangar stopped. |
| `EXTRA_PACKAGES` | | Extra Debian packages to build into the image. |
| `ALLOW_SUDO` | `true` | Let Claude install packages with sudo (it asks you first, every time). |
| `HANGAR_PUBLIC_URL` | browser address | The address you open Hangar at, for SSO. |
| `HANGAR_SECURE_COOKIES` | `false` | Set `true` when you only use HTTPS. |

The full list is in [`.env.example`](.env.example) and [docs/how-it-works.md](docs/how-it-works.md#all-settings).

## More

- [How it works](docs/how-it-works.md): sessions, Codex and local models, tools in the container, all settings, limits.
- [Sign in with SSO](docs/sso.md)
- [Security](docs/security.md)

## Development

FastAPI backend in `hangar/`, plain JavaScript front end in `hangar/static/` (no build step).

```bash
pip install -r requirements.txt -r requirements-dev.txt
HANGAR_PASSWORD=dev-password HANGAR_PORT=7979 python -m hangar   # sign in as admin
python -m pytest -q
```

**Releasing:** set `__version__` in `hangar/__init__.py`, merge to `main`, then `git tag vX.Y.Z && git push origin vX.Y.Z`. The Release workflow checks the version, runs the tests, and publishes the GitHub release that the Proxmox script installs from.

## Credits

[xterm.js](https://xtermjs.org/) (MIT), [marked](https://marked.js.org/) (MIT), and [DOMPurify](https://github.com/cure53/DOMPurify) (Apache-2.0 / MPL-2.0) are vendored. The usage dashboard is adapted from UsageMeter by the same author.

Hangar is an independent project, not made by or affiliated with Anthropic, OpenAI, or Ollama. Claude and Claude Code are trademarks of Anthropic; ChatGPT and Codex are trademarks of OpenAI.

## License

MIT
