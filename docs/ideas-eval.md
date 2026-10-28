# Ideas plugin: Claude behaviour eval

How well Claude uses the Ideas tools in real sessions: does it file new project ideas, avoid duplicates, leave
routine questions alone, mark milestones done, and handle "discard"? The answers come from real headless Claude
Code runs, not mocks.

## Method

- **Harness:** [`tests/eval/ideas_eval.py`](../tests/eval/ideas_eval.py). It starts a throwaway Hangar with
  `HANGAR_PLUGINS=ideas` on a local port, with its own data folder. Then it runs `claude -p` with the same
  arguments Hangar adds to its sessions:
  ```
  --mcp-config '<inline JSON>' --allowedTools mcp__hangar-ideas__* --session-id <new uuid>
  ```
  The Docker image's base arguments (its settings file and container note) were included, as in a real container
  session.
- **Checks:** done against Hangar's API after each run. Projects are attributed to a run by the session ID that
  created them, which the MCP server sends automatically.
- **Setup:** Claude Code 2.1.308 with its default model, `mcp` 2.3.0. Five runs in parallel, each in a fresh
  session (no `CLAUDE.md`, no earlier context).
- **Not edited between runs:** the standing instruction (MCP server instructions in `hangar/ideas/mcp.py`) and the
  tool descriptions were written once and left alone during the eval.

Re-run it with:

```
python tests/eval/ideas_eval.py fresh new repeat routine milestone discard down badtoken
```

The eval ran with the config in a file. Hangar now passes the same JSON inline (see "Notes" below), and the
harness does too.

## Results (2026-10-27)

| Scenario | Pass | Rule |
|---|---|---|
| New idea in a fresh session | **10/10** | Exactly one project, 3-7 milestones, each with a "done when", linked to the session |
| Same idea again, new session, new wording | **10/10** | No new project |
| Routine question, bug fix, or idle brainstorming | **10/10** | Nothing filed and no existing project touched |
| Work matching a milestone (opening prompt from **Work on next milestone**) | **10/10** | That milestone is `done`, and the last one isn't |
| "Discard" (resuming the session that filed it) | **10/10** | Project is `killed` |
| Hangar stopped | **3/3** | Finishes quickly (12-15 s), tells the user the tracker is unavailable, nothing hangs |
| Wrong token | **1/1** | Finishes quickly (14 s), tells the user the token was refused, nothing filed |

The whole run took about 15 minutes. At API prices it would cost about $7.

### Every new-idea run confirmed in one line

Each reply opened with a single sentence naming the project and its number of milestones, plus how to discard it.
After that it went on to the user's request. For example:

> I added **Receipt Renamer CLI** to your tracker with 5 milestones. The first one is a quick test: OCR 10 of your
> real receipts and see whether the date and vendor come out right. The last one is a decision on whether to keep
> it or drop it. Say "discard" if you don't want it tracked.

### Sample milestones

**Voice Memo Journal**: *A tool that transcribes voice memos and turns them into a searchable journal.*

1. Transcription spike works on real memos. *Done when 5 of your actual voice memos are transcribed locally and the transcripts are accurate enough to be useful.*
2. Memos ingest into dated journal entries. *Done when pointing the tool at a folder of memos produces one entry per memo with date, duration, audio link and transcript, with no duplicates on re-run.*
3. Full-text search returns the right entries. *Done when a keyword search finds known phrases and shows matching snippets with dates.*
4. Browsable journal UI. *Done when you can scroll entries by date, open one, read the transcript and play the audio.*
5. Hands-off import of new memos. *Done when a new memo recorded on your phone shows up as a searchable entry without manual steps.*
6. Ship-or-stop decision. *Done when you've used it for two weeks and decided to keep it or shelve it.*

Across the 10 projects, every first milestone was a small test that produces evidence (a spike, a prototype, or
one real input working). Every last milestone was a keep-or-stop decision.

## Prompts

### New idea
1. I've decided to build a small app that tracks my houseplants' watering schedule and reminds me on my phone. Let's do it.
2. New project: I want to make a CLI tool that renames my scanned receipts by date and vendor using OCR.
3. I'm going to start building a browser extension that hides YouTube recommendations during work hours.
4. I want to build a home dashboard on a Raspberry Pi showing the bus times, weather and my calendar.
5. Starting a new side project: a script that backs up my Google Photos to my NAS every night.
6. I'd like to make a board-game score tracker web app for our weekly game night. I'm committing to this one.
7. I'm going to build a tool that turns my voice memos into a searchable journal with transcripts.
8. Project I want to pursue: a budget tracker that imports my bank CSV exports and categorizes spending.
9. I want to build a Discord bot for my running club that logs weekly mileage and posts a leaderboard.
10. Let's start a new project: a personal reading list app that pulls book info from ISBN barcodes.

Titles filed: Houseplant watering tracker · Receipt Renamer CLI · YouTube Focus Extension · Raspberry Pi home
dashboard · Nightly Google Photos → NAS backup · Board-game score tracker · Voice Memo Journal · Budget tracker
from bank CSV exports · Running club Discord mileage bot · Personal reading list app with ISBN barcode lookup.

### Same idea again (new session, different words)
1. I want to build an app that reminds me when to water my houseplants.
2. New idea: a command-line tool that OCRs my receipt scans and renames the files by vendor and date.
3. I want to make a Chrome extension that blocks YouTube's recommended videos while I'm working.
4. I'm going to build a Raspberry Pi dashboard for bus departures, weather and calendar.
5. I want a nightly job that copies my Google Photos library to my NAS.
6. Let's build a web app to keep score at our weekly board game nights.
7. I want to build something that transcribes my voice memos into a journal I can search.
8. I want to write a spending tracker that reads my bank's CSV files and sorts transactions into categories.
9. I'm building a Discord bot for the running club to track weekly miles and show a leaderboard.
10. New project: a reading-list app where I scan a book's barcode and it fills in the details.

Most replies opened by naming the existing project and saying no second one was filed, then continued from that
project's next milestone. Two (3 and 5) went straight into the work instead. In 2 runs (6 and 8) Claude started working and marked the existing project's first milestone
`doing`. That matches the instruction ("mark the next one doing when you begin it").

### Routine (nothing should be filed)
1. My Python script raises KeyError: 'name' when a dict is missing the key. What's the cleanest fix?
2. Why does `git push` say 'Updates were rejected because the tip of your current branch is behind'?
3. How do I make a bash for-loop iterate over files with spaces in their names?
4. My CSS flexbox items won't center vertically. I have display:flex on the parent. What am I missing?
5. There's an off-by-one bug in my loop: for i in range(1, len(items)) skips the first item. How do I fix it?
6. Give me 5 side-project ideas for learning Rust.
7. My Docker container exits immediately after starting. How do I debug it?
8. Would a smart mirror be a fun project? Just thinking out loud, not planning anything.
9. Fix this SQL: SELECT name, COUNT(*) FROM users GROUP BY id; it errors in Postgres.
10. I keep getting 'permission denied (publickey)' when pushing to GitHub. How do I fix it?

Prompts 6 and 8 are near misses: project talk the user hasn't committed to. Neither was filed.

### Milestone done
Each run had its own project, "Greeter script N", with its own folder and three milestones:

1. Script prints a greeting. *Done when `python3 hello.py` prints exactly: hello, ideas*
2. Greeting takes a name. *Done when `python3 hello.py Sam` prints exactly: hello, Sam*
3. Ship or stop.

The first milestone was set to `doing`, as the **Work on next milestone** button does. The session started in
the project folder with that button's opening prompt, using `--permission-mode acceptEdits` so Claude could write
the file. In all 10 runs Claude wrote the script, checked the output, and marked milestone 1 done with an
evidence note, for example *"hello.py created; `python3 hello.py` outputs exactly "hello, ideas" (verified
byte-for-byte)"*. It didn't go on to milestone 2, which the opening prompt didn't ask for.

### Discard
Each new-idea session was resumed with: *"Actually, discard that idea. I don't have time for it."* All 10
projects moved to `killed`, with the reason saved as a note (for example *"User doesn't have time for it."*).

### Failure cases
- **Hangar stopped:** *"I have decided to build a habit tracker app with streaks and weekly reports that syncs
  between my phone and laptop."* Claude replied *"I couldn't file this in your Ideas tracker because Hangar isn't
  reachable right now ... Once it's back up, ask me and I'll add the project"*, then helped with the idea.
  An earlier wording ("a small tool that reminds me to stretch every hour") finished in 30 s, but Claude judged
  it too small to file, so the error path wasn't exercised. That's why the prompt above is bigger.
- **Wrong token:** *"I couldn't file this in your project tracker. Hangar rejected the Ideas token, possibly
  because it was reset. Check the Ideas connection settings in Hangar."*

The MCP server's unit tests cover the same failures directly: unreachable, a server that accepts the connection
and never answers (times out), wrong token, missing token, proxy settings ignored. See
`tests/test_ideas_mcp.py`.

## Notes

- **Config passed inline after the eval:** after the runs, Hangar was changed to pass the MCP config as inline JSON
  instead of a file path. Claude Code refuses to start when an `--mcp-config` file is missing, which would break
  sessions in host-tmux mode. The content is identical. The unit tests and the second manual pass below used the
  inline form.
- **Manual passes in live Hangar sessions:** a session was started through Hangar's own API (tmux, interactive
  Claude), and an idea was typed into the terminal. This was done twice: once with the config file (a meal
  planner), and once with the inline config (a GPX ride tracker). Both times Claude filed one project with 5
  milestones, linked to the session ID Hangar started it with, without asking for permission to use the Ideas
  tools. Claude also confirmed the meal planner in one line. The ride-tracker reply wasn't checked.

## Observations

- **Claude keeps helping after filing:** with an idea plus "let's do it", Claude filed the project and then started
  building. In `-p` runs without write permission it explained that writes were blocked. In an interactive
  session it would ask for permission as usual.
- **No duplicates:** all 10 repeat runs ended with no new project. The eval
  doesn't record whether a run was stopped by `ideas_search` or by the server's own title check, which turns a
  near-duplicate `ideas_create` into "already exists". Either way, nothing was duplicated.
- **Not measured here:** resumed conversations started before the plugin was on (they get the tools on resume),
  and long sessions where the instruction is far back in context.
