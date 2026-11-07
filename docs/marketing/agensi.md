# Agensi listings

Agensi (https://www.agensi.io) sells SKILL.md skills. Listing is free and non-exclusive. Creators keep 70% of each sale, or 90% for buyers who sign up through their affiliate link.

## What we list

| Skill | Folder | Price | Why |
|---|---|---|---|
| session-finder | `skills/session-finder` | Free at launch | Works on its own, offline, no credentials. Free listings build installs and reviews, and each one links back to the repo. |
| hangar-setup | `skills/hangar-setup` | Free | Sends people to the repo. |

Not listed: a plan usage check based on `hangar/usage.py`. It reads Claude Code's OAuth token and calls `api.anthropic.com`. Agensi's scan rejects skills that read local data and also make network calls ("Network access" and "Environment harvesting" checks), so it would have to pass manual review as a false positive.

## Upload

Upload each skill as its own ZIP, with the skill folder at the top level:

```bash
cd skills && for s in session-finder hangar-setup; do zip -r "../$s.zip" "$s" -x '*/__pycache__/*'; done
```

Agensi won't accept the exact same ZIP twice, so change something in the skill before resubmitting a rejected one.

Before submitting, check that each skill passes the scan rules:
- no executables (.exe, .dll, .jar, .wasm)
- no `curl | sh`, `os.system`, `shell=True`, or `chmod 777`
- no secrets in the files
- no network calls in a script that also reads local files

`session-finder` calls `zstd` through `subprocess.run` with an argument list (not a shell), which the scan allows. `hangar-setup` mentions the Proxmox installer but doesn't include the `curl | bash` line, and it tells the agent not to run the installer.

## Listing copy

**session-finder**: Find any past Claude Code or Codex conversation, across every project. Search by what you typed, the folder, or the branch, read the transcript, and get the exact command to resume or fork it. It reads only the logs already on your machine, with no network calls, no API keys, and nothing to install besides Python.

**hangar-setup**: Use Claude Code and Codex from your phone. This skill has your agent install Hangar, a free, self-hosted web UI where every session runs in tmux and keeps going when you close the browser. It covers Docker, Proxmox, plain Linux or macOS, Tailscale, and SSO.

After approval, put the affiliate link in the README's Agent skills section next to the plugin install line.

## Images and packages

Everything to upload is in [`agensi/`](agensi):

| Skill | Package | Logo (512×512) | Screenshots (1600×1000, in order) |
|---|---|---|---|
| session-finder | `session-finder.zip` | `session-finder-logo.png` | `session-finder-1.png` to `-4.png` |
| hangar-setup | `hangar-setup.zip` | `hangar-setup-logo.png` | `hangar-setup-1.png` to `-4.png` |

The session-finder screenshots show the script's real output, run on made-up demo sessions. The hangar-setup screenshots reuse the README screenshots. Rebuild the ZIPs whenever a skill changes (see Upload above).
