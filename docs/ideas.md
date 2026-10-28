# Ideas plugin

Ideas is an optional project tracker built into Hangar. When you describe a new project idea to Claude in a
Hangar session, Claude files it with a short summary and a few milestones, each with a clear "done when". As you
work, Claude marks milestones done. You can browse, edit and start work on projects from the **Ideas** page.

## Turn it on

Set `HANGAR_PLUGINS=ideas` and restart Hangar. With Docker, add it to `.env`:

```
HANGAR_PLUGINS=ideas
```

On first start Hangar creates two files in its data folder:

| File | What it is |
|---|---|
| `ideas.db` | The projects, milestones and notes (SQLite) |
| `ideas-token` | The token Claude's Ideas tools use to reach Hangar. Readable only by you. It works for Ideas and nothing else in Hangar |

Turning the plugin off (removing it from `HANGAR_PLUGINS`) removes the page, the API and the Claude tools. The files
stay, so turning it back on brings everything back.

## In Hangar sessions

Every Claude session Hangar starts or resumes gets the Ideas tools automatically:

```
--mcp-config '{"mcpServers":{"hangar-ideas":{"command":"<python>","args":["-m","hangar.ideas.mcp"],"env":{...}}}}'
--allowedTools mcp__hangar-ideas__*
```

The config says where Hangar is and where the token file is. It doesn't contain the token.

The allow rule means Claude doesn't ask before using the Ideas tools. They can only read and change Ideas data.
The rules Claude follows (search before filing, what's worth filing, how to write milestones) come with the tools
as MCP server instructions, so there's nothing to add to `CLAUDE.md`.

Sessions that were already running when you turned the plugin on get the tools the next time they're started or
resumed.

With `docker-compose.host-tmux.yml`, sessions run in the host's tmux, outside the container. Hangar's Python and data
folder aren't there, so the Ideas tools don't start in those sessions. Claude still starts normally without
them, and the Ideas page still works.

### Check that it works

1. Open a new session and tell Claude about a project you want to start, for example
   *"I want to build a small app that reminds me to water my plants."*
2. Claude replies with one line saying what it filed. The project appears on the **Ideas** page, linked to that
   session.
3. Say *"discard that"* and the project moves to **Killed**.

Run `/mcp` in the session to see the `hangar-ideas` server and its tools.

### Tools

| Tool | What it does |
|---|---|
| `ideas_search` | Find projects that are already tracked. Claude calls it before filing |
| `ideas_create` | File a project with its milestones. A near-duplicate title returns the existing project instead |
| `ideas_get` | Read one project with its milestones |
| `ideas_add_milestones` | Add milestones to a project |
| `ideas_update_milestone` | Mark a milestone `todo`, `doing`, `done` or `dropped` |
| `ideas_set_status` | Move a project between idea, planned, active, paused, shipped and killed |
| `ideas_list` | List projects, for "what am I working on?" |

Each change is linked to the Claude session that made it, using the session ID Claude Code gives MCP servers.

## Claude Code started outside Hangar

On the machine (or container) where Hangar runs, open **Ideas → Connect Claude Code** and run the command it
shows once. It looks like this:

```
claude mcp add --scope user --transport stdio \
  -e HANGAR_URL=http://127.0.0.1:7878 \
  -e PYTHONPATH=/path/to/hangar \
  -e HANGAR_IDEAS_TOKEN=<token> \
  hangar-ideas -- /path/to/python -m hangar.ideas.mcp
```

The command contains the token, so treat it like a password. Hangar sessions don't need this step.

### From another machine

The MCP server is a small Python program that calls Hangar's API, so it can run anywhere that can reach Hangar:

1. Get a copy of this repository and install the MCP package: `pip install mcp==2.3.0`.
2. Run the command above with `HANGAR_URL` set to your Hangar address, `PYTHONPATH` set to the repository folder,
   and the Python that has `mcp` installed.

The token is sent with every request, so only connect over HTTPS or a private network you trust, the same as for
signing in to Hangar.

### Settings the MCP server reads

| Variable | Default | Meaning |
|---|---|---|
| `HANGAR_URL` | `http://127.0.0.1:7878` | Where Hangar is |
| `HANGAR_IDEAS_TOKEN` | | The token |
| `HANGAR_IDEAS_TOKEN_FILE` | `ideas-token` in Hangar's data folder | A file holding the token, used when `HANGAR_IDEAS_TOKEN` isn't set |
| `CLAUDE_CODE_SESSION_ID` | set by Claude Code | Links each change to the session that made it |

The MCP server ignores proxy settings, so the token is only ever sent to `HANGAR_URL`.

## When something goes wrong

The tools return a short error that Claude passes on, instead of hanging:

- **Can't reach Hangar**: Hangar is stopped, restarting, or `HANGAR_URL` is wrong. Requests give up after 10 seconds.
- **Hangar refused the Ideas token**: the token file was replaced or the `-e HANGAR_IDEAS_TOKEN` value is old. Run
  the command from **Connect Claude Code** again.
- **No token found**: the MCP server can't read the token file. Check `HANGAR_IDEAS_TOKEN_FILE`.

To get a new token, stop Hangar, delete `ideas-token` from the data folder, and start Hangar again. Re-run the
connect command anywhere you used the old one.

## Privacy

Ideas makes no outside network calls. Projects, notes and milestones are stored in `ideas.db` in Hangar's data
folder. Claude reads them back through the tools as data. Its instructions say never to follow instructions found
inside them, and the Ideas page shows them as plain text (no images or embedded content).
