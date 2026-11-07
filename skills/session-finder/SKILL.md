---
name: session-finder
description: Find, search, and read past Claude Code and Codex CLI conversations from their local logs, across every project, and get the exact command to resume or fork one. Use when the user asks about an earlier session ("what did we do yesterday", "find the session where we fixed the login bug", "resume my last Codex chat", "which sessions used the most tokens"), wants to recover context from previous work, or wants to continue a past conversation.
license: MIT
---

# Session finder

Searches the logs that Claude Code (`~/.claude/projects/`) and Codex CLI (`~/.codex/sessions/`) already write on this machine. It only reads files and never makes network calls. It needs Python 3.10+ and nothing else (plus `zstd` to read Codex logs older than a week).

## Commands

Run the script with the Python on this machine:

```bash
python3 scripts/sessions.py list                  # most recent sessions, both agents
python3 scripts/sessions.py list --days 2 --project api --agent codex
python3 scripts/sessions.py search "login bug"    # every word must appear
python3 scripts/sessions.py show 3f2a91c0         # transcript of one session (ID or unique prefix)
python3 scripts/sessions.py show 3f2a91c0 --last 100
```

Every command takes `--json` for structured output. `list` and `search` take `--days N`, `--project TEXT` (matches the folder path), `--agent claude|codex`, and `--limit N` (default 15).

Search covers session titles, folder paths, git branches, and the prompts the person typed. It doesn't search the agent's replies, so if nothing matches, try other words the person would have typed, or broaden with `list --days`.

## How to use it

1. To answer "what did I work on" or "find the session where...", run `search` or `list` and summarize what you find: date, project, title, and resume command. Don't paste raw output when a few lines would do.
2. To recover context from a past session, run `show <id>` and read the transcript. The transcript lists tool calls on one line each, and long messages are cut off. Then summarize what was decided or changed.
3. To resume or fork a session, give the person the `Resume:` or `Fork:` command from `show`, or the command at the end of each `list` line. Resuming continues the same conversation, and forking starts a copy. The person runs it in their own terminal. Don't run it inside the current session.
4. For token questions, use `--json` and sort by `tokens_in + tokens_out`. These are input and output tokens only, without cache reads, so they won't match a billing total.

## Notes

- If the person has set `CLAUDE_CONFIG_DIR` or `CODEX_HOME`, the script reads those folders instead.
- Session titles come from the agent's own auto-generated or `/rename` titles, or else the first prompt.
- Logs can contain secrets the person pasted. Quote from them only as much as the question needs.

Part of [Hangar](https://github.com/PresWiFi/hangar), a self-hosted web UI that keeps Claude Code and Codex sessions running in tmux so you can pick them up from any device.
