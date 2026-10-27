"""REST API for Ideas, under /api/ideas. The web UI signs in with the cookie (plus X-Hangar on writes);
the MCP server signs in with the bearer token. Both are checked by Hangar's guard middleware."""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import AfterValidator, BaseModel, Field, StringConstraints

from .. import auth, tmux
from . import repo, token

router = APIRouter(prefix="/api/ideas")
STATIC = Path(__file__).with_name("static")

SESSION_HEADER = "x-claude-session"
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_CTRL_MULTI = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # allows tab and newlines
_CTRL_LINE = re.compile(r"[\x00-\x1f\x7f]")


def _no_control(multiline: bool):
    pattern = _CTRL_MULTI if multiline else _CTRL_LINE

    def check(value):
        if isinstance(value, str) and pattern.search(value):
            raise ValueError("control characters are not allowed")
        return value
    return AfterValidator(check)


def _text(max_length: int, *, multiline: bool = False, required: bool = False):
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=1 if required else 0,
                                            max_length=max_length), _no_control(multiline)]


Title = _text(120, required=True)
Summary = _text(2000, multiline=True)
Criteria = _text(500, multiline=True)
LongText = _text(5000, multiline=True, required=True)
Tag = _text(32, required=True)
PathText = _text(1024)
MilestoneNote = _text(2000, multiline=True)
Status = Literal["idea", "planned", "active", "paused", "shipped", "killed"]
MilestoneStatus = Literal["todo", "doing", "done", "dropped"]


def _unique(tags: list[str] | None) -> list[str] | None:
    return list(dict.fromkeys(tags)) if tags is not None else None


class MilestoneIn(BaseModel):
    title: Title
    done_criteria: Criteria = ""


class ProjectIn(BaseModel):
    title: Title
    summary: Summary = ""
    milestones: Annotated[list[MilestoneIn], Field(max_length=12)] = []
    tags: Annotated[list[Tag], Field(max_length=12), AfterValidator(_unique)] = []
    repo_path: PathText | None = None
    status: Status = "idea"
    force: bool = False


class ProjectPatch(BaseModel):
    title: Title | None = None
    summary: Summary | None = None
    status: Status | None = None
    repo_path: PathText | None = None          # empty string or null clears it
    tags: Annotated[list[Tag], Field(max_length=12), AfterValidator(_unique)] | None = None
    kill_criteria: Summary | None = None
    note: LongText | None = None               # optional note saved with the change (e.g. why it was killed)


class MilestonesIn(BaseModel):
    milestones: Annotated[list[MilestoneIn], Field(min_length=1, max_length=12)]


class MilestonePatch(BaseModel):
    status: MilestoneStatus | None = None
    note: MilestoneNote | None = None
    title: Title | None = None
    done_criteria: Criteria | None = None
    position: Annotated[int, Field(ge=1, le=1000)] | None = None


class NoteIn(BaseModel):
    text: LongText


def _session(request: Request) -> str | None:
    """The Claude Code session making the change, sent by the MCP server. Optional."""
    value = request.headers.get(SESSION_HEADER, "").strip()
    if not value:
        return None
    if not _UUID.fullmatch(value):
        raise HTTPException(400, f"{SESSION_HEADER} must be a session UUID")
    return value.lower()


def _actor(request: Request) -> str:
    return "claude" if token.authorize(request) else "user"


async def _call(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except repo.NotFound as e:
        raise HTTPException(404, str(e))
    except repo.Invalid as e:
        raise HTTPException(400, str(e))


@router.get("/projects")
async def list_projects(status: Status | None = None, q: Annotated[str, Query(max_length=200)] = ""):
    return {"projects": await _call(repo.list_projects, status, q)}


@router.post("/projects")
async def create_project(body: ProjectIn, request: Request):
    return await _call(repo.create_project, title=body.title, summary=body.summary,
                       milestones=[m.model_dump() for m in body.milestones], tags=body.tags,
                       repo_path=body.repo_path, status=body.status, actor=_actor(request),
                       session_id=_session(request), force=body.force)


@router.get("/projects/{ref}")
async def get_project(ref: str):
    return await _call(lambda: repo.get_project(repo.resolve(ref)))


@router.patch("/projects/{ref}")
async def update_project(ref: str, body: ProjectPatch, request: Request):
    session = _session(request)
    fields = body.model_dump(exclude_unset=True)
    note = fields.pop("note", None)
    # Only these two can be cleared; for the rest, null means "leave as is".
    fields = {k: v for k, v in fields.items() if v is not None or k in {"repo_path", "kill_criteria"}}

    def run():
        pid = repo.resolve(ref)
        project = repo.update_project(pid, fields, session)
        return repo.add_note(pid, note, session) if note else project
    return await _call(run)


@router.post("/projects/{ref}/milestones")
async def add_milestones(ref: str, body: MilestonesIn, request: Request):
    session, actor = _session(request), _actor(request)
    items = [m.model_dump() for m in body.milestones]
    return await _call(lambda: repo.add_milestones(repo.resolve(ref), items, actor, session))


@router.patch("/milestones/{mid}")
async def update_milestone(mid: int, body: MilestonePatch, request: Request):
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None or k == "note"}
    return await _call(repo.update_milestone, mid, fields, _session(request))


@router.post("/projects/{ref}/notes")
async def add_note(ref: str, body: NoteIn, request: Request):
    session = _session(request)
    return await _call(lambda: repo.add_note(repo.resolve(ref), body.text, session))


class StartIn(BaseModel):
    cwd: PathText | None = None    # folder to use when the project has none (or to override it)
    save_folder: bool = False      # remember cwd as the project's folder


@router.post("/projects/{ref}/start-session")
async def start_session(ref: str, body: StartIn, request: Request):
    """Open a Claude session in the project's folder, seeded with its summary and next milestone.
    Web UI only: the MCP token can't start sessions."""
    if not auth.check_token(request.cookies.get(auth.COOKIE)):
        raise HTTPException(403, "Starting sessions needs the web UI sign-in")

    def run():
        pid = repo.resolve(ref)
        project = repo.get_project(pid)
        folder = repo.clean_repo_path(body.cwd or project["repo_path"])
        if not folder:
            raise HTTPException(409, "This project has no folder yet. Pick one.")
        if not os.path.isdir(folder):
            raise repo.Invalid(f"Folder not found: {folder}")
        if body.save_folder and body.cwd and folder != project["repo_path"]:
            repo.update_project(pid, {"repo_path": folder})
        try:
            started = tmux.new_session(folder, label=project["title"][:60], prompt=repo.opening_prompt(project))
        except tmux.TmuxError as e:
            raise repo.Invalid(str(e))
        repo.mark_started(pid, started.get("claude_id"))
        return started
    return await _call(run)


# The plugin's front end, served only when the plugin is on (and only to signed-in users).

@router.get("/ui.js")
async def ui_js():
    return FileResponse(STATIC / "ideas.js", media_type="text/javascript")


@router.get("/ui.css")
async def ui_css():
    return FileResponse(STATIC / "ideas.css", media_type="text/css")
