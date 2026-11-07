# Hangar launch copy

Drafts for review. No posts have been published. Verify each destination's current rules before submitting, disclose the poster's actual relationship to Hangar, and use one genuine account. These drafts intentionally make no request for votes or stars.

Repository: https://github.com/PresWiFi/hangar

## Core message

**Park your coding agent sessions. Pick them back up from any device.**

Hangar is an MIT-licensed, self-hosted web UI for Claude Code and Codex CLI. Sessions run in tmux, so closing your browser leaves them running on your server. Reopen the terminal or read the transcript from a laptop, phone, or tablet.

The strongest demonstration is the same session continuing across two devices. Lead with that workflow; introduce the other features only when relevant.

## Hacker News: human briefing only

Do not submit AI-generated or AI-edited text to Hacker News. Its current guidelines prohibit it. A human who can answer technical questions must independently write any submission and comments, after reading the project and testing it. The material below is a fact inventory, not submission copy or a title to adapt.

- Repository: https://github.com/PresWiFi/hangar
- MIT-licensed self-hosted web UI for Claude Code and Codex CLI.
- tmux owns the running session; closing the browser does not stop it.
- Live terminal, readable transcript, phone controls, searchable sessions, resume and fork.
- Docker, Proxmox LXC, and native Python installation options.
- Backend: FastAPI. Front end: plain JavaScript, no build step.
- Single owner; authenticated users receive shell access. Documented access is LAN or VPN.
- Container stops end tmux processes. Conversation persistence and restore are separate from keeping a live process running.
- Subscription and local-model integrations have documented caveats in `docs/how-it-works.md`.

A human should verify Show HN eligibility and current site rules, disclose their actual role, and independently explain a real demonstration. Do not solicit votes or coordinate engagement.

## r/selfhosted: new-project megathread comment

Use only the current New Project Megathread while Hangar is less than three months old; this is not a standalone-post draft. Recheck the [distribution queue](distribution.md) and live community rules. Add a truthful relationship disclosure at the start before posting. Confirm the release is ready for others to install.

**Opening line:** Hangar: a self-hosted web UI for persistent Claude Code and Codex sessions

**Body:**

Hangar lets you start a coding-agent session on your server, close the browser, and return to the same session from your phone or another computer. Sessions run in tmux, with both a live terminal and a readable transcript in the web UI.

For a homelab setup, it offers Docker and Proxmox LXC installation, one owner account, optional OpenID Connect SSO, and documented LAN/Tailscale access. It can use Claude or ChatGPT subscription sign-in, or local models through Ollama and llama.cpp. Local-model capability depends on the model and server configuration.

Security context: anyone signed in has shell access to the machine. The documentation recommends a LAN or VPN and says not to expose port 7878 to the internet. Mount only the projects the container needs.

Source, screenshots, and setup: https://github.com/PresWiFi/hangar

MIT license. Feedback welcome on deployment friction, mobile controls, and how this fits alongside an existing SSH/tmux setup.

## r/ClaudeCode: hold for destination verification

The distribution review verified r/ClaudeAI, not r/ClaudeCode. Do not submit this draft until r/ClaudeCode rules and eligibility have been checked. Do not reuse it unchanged in r/ClaudeAI: that community requires a real builder story and details of Claude's role, an eligible existing account, and suitable flair. Those facts must come from the actual builder. Add a truthful relationship disclosure before posting.

**Title:** Hangar: reopen a self-hosted Claude Code session from your phone

**Body:**

Hangar is a self-hosted browser UI around Claude Code sessions running in tmux. Closing the browser leaves the session running; you can reopen it on another device and switch between the live terminal and a readable conversation transcript.

The phone UI includes controls for Esc, Shift+Tab, arrows, and Ctrl+C, plus a compose box. Sessions are searchable, and past conversations can be resumed or forked. It also supports Codex CLI and local-model servers.

Claude sign-in uses your Claude Pro or Max plan. Hangar has one owner account and grants that account shell access, so use it over a LAN or VPN as documented.

Code, screenshots, and installation: https://github.com/PresWiFi/hangar

What part of using Claude Code away from your main keyboard is most awkward: terminal input, finding the right session, or reading tool output?

## Short social post

Hangar keeps Claude Code and Codex sessions running in tmux behind a self-hosted web UI. Close your laptop, reopen the same session on your phone. Terminal + transcript, Docker or Proxmox setup, MIT licensed. https://github.com/PresWiFi/hangar

Attach the real device-handoff recording below. Include a truthful relationship disclosure in the post or an immediately visible accompanying sentence where needed. Do not describe this as a personal experience unless it is.

## Technical article outline

**Working title:** A browser window into tmux: how Hangar keeps coding-agent sessions accessible

1. **The workflow:** a coding task lives on one machine while its owner switches devices. Show a real session on desktop and phone; explain which process continues when the browser closes.
2. **Process and browser lifetimes:** trace browser terminal → WebSocket bridge → tmux → Claude Code or Codex. Closing a tab does not stop tmux. A container stop does, so explain resume and automatic restore separately.
3. **Terminal versus transcript:** explain why both views exist. Hangar incrementally reads Claude conversation files without modifying them; Codex sessions are linked to their log files after startup.
4. **Provider choices:** show subscription login and the local-server options without implying model parity. Explain that a local model's tool use and context requirements matter.
5. **A small reproducible setup:** use the README's Docker commands, choose a disposable example project, and demonstrate reconnecting. Link the native and Proxmox alternatives instead of duplicating every installer.
6. **The trust boundary:** one owner, shell access, workspace mounts, LAN/VPN access. Explain that the folder picker is not a terminal sandbox. Mention that scheduled tasks run unattended with elevated agent permissions if discussing automation.
7. **Implementation tradeoffs:** FastAPI, plain JavaScript, tmux, and upstream CLI logs. Explain the unofficial usage endpoints if showing usage cards; do not promise stable provider integrations.
8. **Invite useful feedback:** ask readers to try the device handoff and report setup failures with deployment type and sanitized logs. Link the repository and security documentation.

Evidence to gather before publication: tested commit, environment, actual install steps, a real recording, and any observed caveats. Do not add performance figures without a reproducible measurement.

## 45-second real demo storyboard

Record an actual instance using a disposable repository and no private prompts, account details, tokens, or personal files. Use two real device views or clearly label a mobile browser emulation. Do not represent an edited wait as instantaneous execution.

| Time | Picture | Narration or caption |
|---|---|---|
| 0–5s | Desktop Hangar sidebar; open an existing demo session. | “Hangar is a self-hosted web UI for Claude Code and Codex.” |
| 5–12s | Submit a safe real prompt, such as asking the agent to inspect the demo repository and describe its structure. Show genuine output beginning. | “Each session runs in tmux on your machine.” |
| 12–17s | Close the desktop browser tab. | “Closing the browser leaves the session running.” |
| 17–28s | Open the same Hangar instance on a phone over LAN or VPN, sign in beforehand, and select the same session. | “Open it from another device and pick up the same session.” |
| 28–36s | Show the real terminal output, then switch to the transcript. | “Use the live terminal, or read the conversation transcript.” |
| 36–41s | Show the mobile key bar and compose box without interrupting an active task unnecessarily. | “Phone controls cover the keys a terminal needs.” |
| 41–45s | End card with repository URL and a dashboard screenshot from the recording. | “Hangar. Self-hosted. MIT licensed. github.com/PresWiFi/hangar” |

If the task finishes before the phone connects, show the completed output honestly: process continuity does not require a long artificial task. Export one captioned video and one short silent clip using existing free tools; no paid distribution or subscriptions are required.

## Claim sources and review checks

- Product behavior, features, licensing, install options: [README](../../README.md).
- Session lifecycle, provider behavior, restoration, local models, implementation: [How it works](../how-it-works.md).
- One-owner access, shell privileges, mounts, LAN/VPN advice, unattended tasks: [Security](../security.md).
- Browser closure and container restart are different events. Never promise that processes survive a container stop.
- “Self-hosted” describes Hangar; subscription-backed Claude and Codex still use their providers. Do not claim all inference or data stays local.
- No claims of superiority, user counts, performance, provider endorsement, or universal compatibility are substantiated here.
- The $0 marketing budget does not imply free hardware, free hosted inference, or free Claude/ChatGPT plans.
