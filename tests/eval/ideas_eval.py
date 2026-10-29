"""Ideas plugin eval: real headless Claude Code runs against a throwaway Hangar with the Ideas plugin on.

This uses your Claude account (about 50 runs for the full set). Run it from the repo root:

    python tests/eval/ideas_eval.py fresh new repeat routine milestone discard down badtoken

Scenarios run in that order: `repeat` re-mentions the ideas `new` filed, and `discard` resumes the `new` sessions.
Results go to <work dir>/results.jsonl and one PASS/FAIL line per run is printed. The Python running this needs
Hangar's requirements (including mcp). Set IDEAS_EVAL_DIR to keep the work folder somewhere specific, and
IDEAS_EVAL_BASE_ARGS to add the arguments your Hangar install passes to Claude (for example the Docker image's
HANGAR_CLAUDE_BASE_ARGS).
"""
import concurrent.futures as cf
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

APP = Path(__file__).resolve().parents[2]
PY = sys.executable
HERE = Path(os.environ.get("IDEAS_EVAL_DIR") or Path(tempfile.gettempdir()) / "hangar-ideas-eval")
DATA, WS = HERE / "data", HERE / "ws"
PORT = int(os.environ.get("IDEAS_EVAL_PORT", "7980"))
URL = f"http://127.0.0.1:{PORT}"
STATE = HERE / "state.json"
RESULTS = HERE / "results.jsonl"
BASE_ARGS = shlex.split(os.environ.get("IDEAS_EVAL_BASE_ARGS", ""))
PARALLEL = 5
TIMEOUT = 300

NEW = [
    "I've decided to build a small app that tracks my houseplants' watering schedule and reminds me on my phone. Let's do it.",
    "New project: I want to make a CLI tool that renames my scanned receipts by date and vendor using OCR.",
    "I'm going to start building a browser extension that hides YouTube recommendations during work hours.",
    "I want to build a home dashboard on a Raspberry Pi showing the bus times, weather and my calendar.",
    "Starting a new side project: a script that backs up my Google Photos to my NAS every night.",
    "I'd like to make a board-game score tracker web app for our weekly game night. I'm committing to this one.",
    "I'm going to build a tool that turns my voice memos into a searchable journal with transcripts.",
    "Project I want to pursue: a budget tracker that imports my bank CSV exports and categorizes spending.",
    "I want to build a Discord bot for my running club that logs weekly mileage and posts a leaderboard.",
    "Let's start a new project: a personal reading list app that pulls book info from ISBN barcodes.",
]
REPEAT = [
    "I want to build an app that reminds me when to water my houseplants.",
    "New idea: a command-line tool that OCRs my receipt scans and renames the files by vendor and date.",
    "I want to make a Chrome extension that blocks YouTube's recommended videos while I'm working.",
    "I'm going to build a Raspberry Pi dashboard for bus departures, weather and calendar.",
    "I want a nightly job that copies my Google Photos library to my NAS.",
    "Let's build a web app to keep score at our weekly board game nights.",
    "I want to build something that transcribes my voice memos into a journal I can search.",
    "I want to write a spending tracker that reads my bank's CSV files and sorts transactions into categories.",
    "I'm building a Discord bot for the running club to track weekly miles and show a leaderboard.",
    "New project: a reading-list app where I scan a book's barcode and it fills in the details.",
]
ROUTINE = [
    "My Python script raises KeyError: 'name' when a dict is missing the key. What's the cleanest fix?",
    "Why does `git push` say 'Updates were rejected because the tip of your current branch is behind'?",
    "How do I make a bash for-loop iterate over files with spaces in their names?",
    "My CSS flexbox items won't center vertically. I have display:flex on the parent. What am I missing?",
    "There's an off-by-one bug in my loop: for i in range(1, len(items)) skips the first item. How do I fix it?",
    "Give me 5 side-project ideas for learning Rust.",
    "My Docker container exits immediately after starting. How do I debug it?",
    "Would a smart mirror be a fun project? Just thinking out loud, not planning anything.",
    "Fix this SQL: SELECT name, COUNT(*) FROM users GROUP BY id; it errors in Postgres.",
    "I keep getting 'permission denied (publickey)' when pushing to GitHub. How do I fix it?",
]
DISCARD = "Actually, discard that idea. I don't have time for it."


# --- server and API ---

def start_server(token_ok=True):
    stop_server()
    HERE.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    WS.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HANGAR_", "CLAUDE"))}
    env.update(HANGAR_DATA_DIR=str(DATA), HANGAR_PASSWORD="dev", HANGAR_PORT=str(PORT), HANGAR_HOST="127.0.0.1",
               HANGAR_WORKSPACES=str(WS), HANGAR_TMUX_SOCKET=str(Path(tempfile.gettempdir()) / f"hgev-{PORT}.sock"), HANGAR_PLUGINS="ideas")
    log = open(HERE / "server.log", "a")
    p = subprocess.Popen([PY, "-m", "hangar"], cwd=APP, env=env, stdout=log, stderr=log, start_new_session=True)
    (HERE / "server.pid").write_text(str(p.pid))
    for _ in range(100):
        try:
            urllib.request.urlopen(URL + "/api/me", timeout=1)
        except urllib.error.HTTPError:
            break  # 401 means it's up
        except Exception:
            time.sleep(0.2)
    else:
        raise SystemExit("server didn't start")


def stop_server():
    pid = HERE / "server.pid"
    if pid.exists():
        try:
            os.killpg(int(pid.read_text()), signal.SIGTERM)
        except ProcessLookupError:
            pass
        pid.unlink()
        for _ in range(50):
            try:
                urllib.request.urlopen(URL + "/api/me", timeout=0.5)
            except urllib.error.HTTPError:
                time.sleep(0.1)
                continue
            except Exception:
                break


def api(method, path, body=None):
    token = (DATA / "ideas-token").read_text().strip()
    req = urllib.request.Request(URL + "/api/ideas" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def projects():
    return api("GET", "/projects")["projects"]


def detail(pid):
    return api("GET", f"/projects/{pid}")


# --- running Claude ---

def claude_env():
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("HANGAR_", "CLAUDE_CODE_", "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT"))}


def hangar_mcp_config(token_file=None):
    """The same --mcp-config value Hangar passes to its sessions (see hangar/ideas/connect.py)."""
    env = {"HANGAR_URL": URL, "PYTHONPATH": str(APP),
           "HANGAR_IDEAS_TOKEN_FILE": str(token_file or DATA / "ideas-token")}
    return json.dumps({"mcpServers": {"hangar-ideas": {"command": PY, "args": ["-m", "hangar.ideas.mcp"], "env": env}}})


def run_claude(prompt, cwd, *, session=None, resume=None, extra=(), persist=False, mcp_config=None):
    args = ["claude", *BASE_ARGS, "--mcp-config", mcp_config or hangar_mcp_config(),
            "--allowedTools", "mcp__hangar-ideas__*", *extra]
    if resume:
        args += ["--resume", resume]
    else:
        args += ["--session-id", session]
    if not persist:
        args += ["--no-session-persistence"]
    args += ["-p", "--output-format", "json", "--", prompt]
    start = time.time()
    try:
        out = subprocess.run(args, cwd=cwd, env=claude_env(), capture_output=True, text=True, timeout=TIMEOUT)
        elapsed = time.time() - start
        try:
            data = json.loads(out.stdout)
        except ValueError:
            return {"reply": "", "error": (out.stderr or out.stdout)[-800:], "seconds": round(elapsed, 1)}
        return {"reply": data.get("result", ""), "error": "" if not data.get("is_error") else "is_error",
                "seconds": round(elapsed, 1), "turns": data.get("num_turns"), "cost": data.get("total_cost_usd")}
    except subprocess.TimeoutExpired:
        return {"reply": "", "error": "timeout", "seconds": TIMEOUT}


def record(scenario, i, prompt, result, passed, why):
    row = {"scenario": scenario, "i": i, "prompt": prompt, "pass": passed, "why": why, **result}
    with RESULTS.open("a") as f:
        f.write(json.dumps(row) + "\n")
    mark = "PASS" if passed else "FAIL"
    print(f"[{scenario} {i}] {mark} {why} ({result.get('seconds')}s)", flush=True)
    return row


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(state):
    STATE.write_text(json.dumps(state, indent=2))


def folder(name):
    d = WS / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def by_session(sid):
    return [p for p in projects() if p["source_session_id"] == sid]


# --- scenarios ---

def scenario_new():
    state = load_state()
    state["new"] = {}

    def one(i, prompt):
        sid = str(uuid.uuid4())
        res = run_claude(prompt, folder("chat"), session=sid, persist=True)
        created = by_session(sid)
        why, passed = "", False
        if len(created) != 1:
            why = f"{len(created)} projects created"
        else:
            p = detail(created[0]["id"])
            ms = p["milestones"]
            sessions = [s["session_id"] for s in p["sessions"]]
            if not 3 <= len(ms) <= 7:
                why = f"{len(ms)} milestones"
            elif not all(m["done_criteria"].strip() for m in ms):
                why = "milestone without done criteria"
            elif sessions != [sid]:
                why = f"session links {sessions}"
            else:
                passed, why = True, f"'{p['title']}', {len(ms)} milestones"
            state["new"][str(i)] = {"session": sid, "project": p["id"]}
        return record("new", i, prompt, res, passed, why)

    with cf.ThreadPoolExecutor(PARALLEL) as ex:
        list(ex.map(lambda a: one(*a), enumerate(NEW, 1)))
    save_state(state)


def scenario_repeat():
    def one(i, prompt):
        before = {p["id"] for p in projects()}
        sid = str(uuid.uuid4())
        res = run_claude(prompt, folder("chat"), session=sid)
        mine = by_session(sid)
        linked = [p["id"] for p in projects() if p["id"] in before
                  and sid in [s["session_id"] for s in detail(p["id"])["sessions"]]]
        passed = not mine
        why = "no duplicate" if passed else f"created {[p['title'] for p in mine]}"
        if linked:
            why += f"; touched existing project(s) {linked}"
        return record("repeat", i, prompt, res, passed, why)

    with cf.ThreadPoolExecutor(PARALLEL) as ex:
        list(ex.map(lambda a: one(*a), enumerate(REPEAT, 1)))


def scenario_routine():
    def one(i, prompt):
        sid = str(uuid.uuid4())
        res = run_claude(prompt, folder("chat"), session=sid)
        mine = by_session(sid)
        touched = [p["id"] for p in projects() if sid in [s["session_id"] for s in detail(p["id"])["sessions"]]]
        passed = not mine and not touched
        why = "nothing filed" if passed else f"filed/touched {[p['title'] for p in mine] or touched}"
        return record("routine", i, prompt, res, passed, why)

    with cf.ThreadPoolExecutor(PARALLEL) as ex:
        list(ex.map(lambda a: one(*a), enumerate(ROUTINE, 1)))


def opening_prompt(project):
    """Same text Hangar's "Work on next milestone" button sends."""
    sys.path.insert(0, str(APP))
    os.environ.setdefault("HANGAR_DATA_DIR", str(HERE / "harness-data"))
    from hangar.ideas.repo import opening_prompt as op
    return op(project)


def scenario_milestone():
    opening_prompt  # imported lazily below; warm it up before the threads start
    sys.path.insert(0, str(APP))
    os.environ.setdefault("HANGAR_DATA_DIR", str(HERE / "harness-data"))
    import hangar.ideas.repo  # noqa: F401

    def one(i):
        path = folder(f"greeter-{i}")
        for f in Path(path).iterdir():
            f.unlink()
        p = api("POST", "/projects", {
            "title": f"Greeter script {i}", "force": True, "repo_path": path,
            "summary": "A tiny command-line greeter, to try out the tracker.",
            "milestones": [
                {"title": "Script prints a greeting", "done_criteria": "`python3 hello.py` prints exactly: hello, ideas"},
                {"title": "Greeting takes a name", "done_criteria": "`python3 hello.py Sam` prints exactly: hello, Sam"},
                {"title": "Ship or stop", "done_criteria": "Decide whether the greeter is worth keeping"}]})["project"]
        first = p["milestones"][0]["id"]
        api("PATCH", f"/milestones/{first}", {"status": "doing"})  # what the button does
        prompt = opening_prompt(detail(p["id"]))
        res = run_claude(prompt, path, session=str(uuid.uuid4()),
                         extra=["--permission-mode", "acceptEdits", "--allowedTools", "Bash(python3:*)"])
        ms = detail(p["id"])["milestones"]
        status = [m["status"] for m in ms]
        passed = status[0] == "done" and status[2] != "done"
        why = f"statuses {status}" + (f"; note: {ms[0]['note']!r}" if ms[0]["note"] else "")
        return record("milestone", i, prompt, res, passed, why)

    with cf.ThreadPoolExecutor(PARALLEL) as ex:
        list(ex.map(one, range(1, 11)))


def scenario_discard():
    state = load_state()

    def one(i, info):
        res = run_claude(DISCARD, folder("chat"), resume=info["session"], persist=True)
        p = detail(info["project"])
        passed = p["status"] == "killed"
        why = f"status {p['status']}" + (f"; note {p['notes'][0]['text']!r}" if p["notes"] else "")
        return record("discard", int(i), DISCARD, res, passed, why)

    with cf.ThreadPoolExecutor(PARALLEL) as ex:
        list(ex.map(lambda a: one(*a), state["new"].items()))


def scenario_down():
    stop_server()
    prompt = "I have decided to build a habit tracker app with streaks and weekly reports that syncs between my phone and laptop."
    res = run_claude(prompt, folder("chat"), session=str(uuid.uuid4()))
    passed = res["seconds"] < 120 and not res["error"]
    record("down", 1, prompt, res, passed, f"finished in {res['seconds']}s")
    start_server()


def scenario_badtoken():
    bad = HERE / "bad-token"
    bad.write_text("not-the-token\n")
    path = hangar_mcp_config(bad)
    prompt = "I want to build a little app that tracks which friends I owe a call to and nudges me weekly."
    sid = str(uuid.uuid4())
    res = run_claude(prompt, folder("chat"), session=sid, mcp_config=path)
    passed = res["seconds"] < 120 and not res["error"] and not by_session(sid)
    record("badtoken", 1, prompt, res, passed, f"finished in {res['seconds']}s")



if __name__ == "__main__":
    print(f"Work folder: {HERE}", flush=True)
    if "fresh" in sys.argv:
        stop_server()
        shutil.rmtree(DATA, ignore_errors=True)
        for f in (STATE, RESULTS):
            f.unlink(missing_ok=True)
    start_server()
    for name in sys.argv[1:]:
        if name not in {"fresh", "keep"}:
            print(f"== {name}", flush=True)
            globals()[f"scenario_{name}"]()
    if "keep" not in sys.argv:
        stop_server()
