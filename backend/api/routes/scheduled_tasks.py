from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from backend.api.deps import get_current_user
from backend.core.database import get_db
from backend.core.models import Conversation, ScheduledTask, ScheduledTaskRunLog, User
from backend.runtime.task_execution import _compute_next_cron_run

router = APIRouter(prefix="/scheduled_tasks", tags=["scheduled_tasks"])


class CreateScheduledTaskRequest(BaseModel):
    conversation_id: str
    title: str = Field(default="Scheduled Task", min_length=1, max_length=255)
    prompt: str = Field(min_length=1)
    schedule_type: str = Field(pattern="^(once|interval|cron)$")
    run_at: datetime | None = None
    interval_seconds: int | None = None
    cron_expression: str | None = None


class UpdateScheduledTaskRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    prompt: str | None = None
    status: str | None = Field(default=None, pattern="^(active|paused|completed)$")


def _serialize_task(task: ScheduledTask) -> dict:
    return {
        "id": task.id,
        "owner_user_id": task.owner_user_id,
        "conversation_id": task.conversation_id,
        "title": task.title,
        "prompt": task.prompt,
        "schedule_type": task.schedule_type,
        "schedule_value": task.schedule_value,
        "status": task.status,
        "next_run": task.next_run.isoformat() if task.next_run else None,
        "last_run": task.last_run.isoformat() if task.last_run else None,
        "last_result": task.last_result,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


def _serialize_log(log: ScheduledTaskRunLog) -> dict:
    return {
        "id": log.id,
        "task_id": log.task_id,
        "run_at": log.run_at.isoformat() if log.run_at else None,
        "duration_ms": log.duration_ms,
        "status": log.status,
        "result": log.result,
        "error": log.error,
    }


@router.get("")
def list_tasks(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    tasks = db.scalars(
        select(ScheduledTask)
        .where(ScheduledTask.owner_user_id == current_user.id)
        .order_by(desc(ScheduledTask.created_at))
    ).all()
    return {"items": [_serialize_task(task) for task in tasks]}


@router.post("")
def create_task(
    payload: CreateScheduledTaskRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conversation = db.get(Conversation, payload.conversation_id)
    if not conversation or conversation.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if payload.schedule_type == "once":
        next_run = payload.run_at or (datetime.now(timezone.utc) + timedelta(minutes=1))
        schedule_value = next_run.isoformat()
    elif payload.schedule_type == "interval":
        interval_seconds = int(payload.interval_seconds or 0)
        if interval_seconds <= 0:
            raise HTTPException(status_code=400, detail="interval_seconds must be greater than 0")
        next_run = datetime.now(timezone.utc) + timedelta(seconds=interval_seconds)
        schedule_value = str(interval_seconds)
    else:
        expression = str(payload.cron_expression or "").strip()
        next_run = _compute_next_cron_run(expression)
        if not next_run:
            raise HTTPException(status_code=400, detail="Invalid cron_expression")
        schedule_value = expression

    task = ScheduledTask(
        owner_user_id=current_user.id,
        conversation_id=payload.conversation_id,
        title=payload.title.strip(),
        prompt=payload.prompt,
        schedule_type=payload.schedule_type,
        schedule_value=schedule_value,
        status="active",
        next_run=next_run,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return {"item": _serialize_task(task)}


@router.patch("/{task_id}")
def update_task(
    task_id: str,
    payload: UpdateScheduledTaskRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    task = db.get(ScheduledTask, task_id)
    if not task or task.owner_user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    if payload.title is not None:
        task.title = payload.title.strip()
    if payload.prompt is not None:
        task.prompt = payload.prompt
    if payload.status is not None:
        task.status = payload.status
    db.add(task)
    db.commit()
    db.refresh(task)
    return {"item": _serialize_task(task)}


@router.delete("/{task_id}")
def delete_task(task_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    task = db.get(ScheduledTask, task_id)
    if not task or task.owner_user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    db.delete(task)
    db.commit()
    return {"deleted": True, "id": task_id}


@router.get("/{task_id}/logs")
def list_task_logs(task_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    task = db.get(ScheduledTask, task_id)
    if not task or task.owner_user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    logs = db.scalars(
        select(ScheduledTaskRunLog)
        .where(ScheduledTaskRunLog.task_id == task_id)
        .order_by(desc(ScheduledTaskRunLog.run_at))
    ).all()
    return {"items": [_serialize_log(log) for log in logs]}
