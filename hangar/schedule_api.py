"""/api/schedules: scheduled tasks (see schedules.py)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import schedules

router = APIRouter(prefix="/api/schedules")


class TaskIn(BaseModel):
    name: str = ""
    cwd: str = ""
    agent: str = "claude"
    model: str = ""
    ollama_server: str = ""   # run on a local model server (Ollama or llama.cpp) instead
    ollama_model: str = ""
    prompt: str = ""
    schedule: dict = {}
    enabled: bool = True


async def _run(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except KeyError:
        raise HTTPException(404, "No such task")
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


def _one(task_id: str) -> dict:
    return next(t for t in schedules.all_views() if t["id"] == task_id)


@router.get("")
async def list_tasks():
    return {"tasks": await _run(schedules.all_views), "timezone": schedules.timezone()}


@router.post("")
async def create_task(body: TaskIn):
    task = await _run(schedules.create, body.model_dump())
    return await _run(_one, task["id"])


@router.put("/{task_id}")
async def update_task(task_id: str, body: TaskIn):
    await _run(schedules.update, task_id, body.model_dump())
    return await _run(_one, task_id)


class EnabledIn(BaseModel):
    enabled: bool


@router.post("/{task_id}/enabled")
async def set_enabled(task_id: str, body: EnabledIn):
    task = schedules.get(task_id)
    if not task:
        raise HTTPException(404, "No such task")
    await _run(schedules.update, task_id, {**task, "enabled": body.enabled})
    return await _run(_one, task_id)


@router.post("/{task_id}/run")
async def run_now(task_id: str):
    entry = await _run(schedules.run, task_id)
    return {"run": entry, "task": await _run(_one, task_id)}


@router.delete("/{task_id}")
async def delete_task(task_id: str):
    if not schedules.get(task_id):
        raise HTTPException(404, "No such task")
    await _run(schedules.delete, task_id)
    return {"ok": True}
