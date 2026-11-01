# Hangar: road to 100 stars

Owner: marketing project manager (this agent). Budget: $0. Target: 100 genuine GitHub stars. Baseline: 1 star, verified from GitHub API on 2026-10-31. No deadline supplied; this first 14-day campaign is an experiment, not a forecast.

## Positioning

Keep your coding agent sessions running. Pick them back up from your phone.

Hangar is a self-hosted browser interface for Claude Code and Codex CLI, with tmux-backed sessions, readable transcripts and a plan-usage dashboard. Lead with a specific workflow: start on a laptop, close the browser, reopen the same session on a phone. Docker users who already run coding agents on a home server are the first audience; homelab and Proxmox users are the second.

Avoid promising free model access, unlimited usage, team isolation or uninterrupted execution through host failures. Hangar itself is MIT licensed; model access has separate requirements. Preserve README security guidance.

## First 14 days

| Window | Action | Evidence required |
|---|---|---|
| Day 0 | Record baseline; prepare README navigation and optional star request | API observation and reviewable diff |
| Days 1–2 | Verify Docker onboarding in a clean environment; record a real 45-second desktop-to-phone demo | OS/version, commands, results and video without credentials |
| Days 3–5 | Publish to one eligible community using its current rules; answer questions and fix setup blockers | Authorized account, post URL, linked fixes |
| Days 6–7 | Publish a technical walkthrough on an available owned channel | Authorized account and article URL |
| Days 8–10 | Consider curated lists once age and activity requirements are met | Eligibility evidence and submission URL |
| Days 11–14 | Review results; share a substantive update if there is new evidence | Metric changes and next decision |

Read [distribution.md](distribution.md) before selecting a channel. Research has identified eligibility gates; launch dates must follow those gates. HN requires human-written posts, so do not submit generated launch copy there.

## Measurement

The outcome is total stars. Secondary signals: unique repository visitors, clones, voluntarily reported successful installs, useful issues and repeat use. Do not infer installs from clones or claim exact per-channel star attribution.

Record `gh api repos/PresWiFi/hangar --jq .stargazers_count` with a UTC timestamp in [metrics.csv](metrics.csv). If permitted, capture `/repos/PresWiFi/hangar/traffic/views` and `/traffic/clones` regularly; GitHub retains limited traffic history. Record each publication URL, channel, UTC time, 24/72-hour star counts and user feedback. No paid analytics is needed.

Planning assumption only: 99 additional stars at a hypothetical 5% visitor-to-star conversion would require about 1,980 qualified visitors; at 2%, 4,950. There is no evidence yet for Hangar's conversion rate. Replace assumptions with observations.

If visitors cannot install, fix onboarding before further promotion. If there are few visits, revise the hook or channel. If installs do not lead to repeat use, prioritize product feedback. Review at 10, 25, 50 and 100 stars; checkpoints are not forecasts.

## Campaign log

| Action | Status | Evidence |
|---|---|---|
| Baseline | Complete | 1 star; metrics.csv |
| Audit | Complete | Screenshots and install docs exist; About description omits Codex |
| Repository metadata | Complete | Description includes Codex; added codex/homelab/proxmox topics |
| README improvements | Prepared | Campaign diff |
| Launch materials | Prepared separately | launch-copy.md |
| Channel research | Prepared separately | distribution.md |
| Clean install and real demo | Pending | Verification must precede claims of tested onboarding |
| Public distribution | Pending posting authorization and account access | No launch posts sent |

## Applied metadata

Description: `Self-hosted web UI for Claude Code and Codex CLI. Persistent tmux sessions, mobile access, transcripts, and plan usage. Docker and Proxmox.`

Kept existing topics and added `codex`, `homelab`, `proxmox`. Description and topics applied on 2026-10-31.

## Materials

- [Launch copy and demo storyboard](launch-copy.md)
- [Channel research](distribution.md)
- [Metrics](metrics.csv)
