"""JSON API — Webhook Schedules CRUD."""

import logging
from datetime import datetime, timezone

from croniter import croniter
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from storage.webhook_schedules import (
    create_schedule,
    delete_schedule,
    get_all_schedules,
    get_schedule,
    update_schedule,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Schedules ────────────────────────────────────────────────────────────────


class ScheduleIn(BaseModel):
    name: str
    cron_expression: str
    enabled: bool = True
    webhook_endpoint_id: str | None = None
    telegram_channel_id: str | None = None
    twitter_account_id: str | None = None
    query_params: dict | None = None
    max_articles: int = 1

    @field_validator("cron_expression")
    @classmethod
    def validate_cron(cls, v: str) -> str:
        try:
            croniter(v)
        except Exception as e:
            raise ValueError(f"Invalid cron expression: {e}")
        return v


class ScheduleUpdate(BaseModel):
    name: str | None = None
    cron_expression: str | None = None
    enabled: bool | None = None
    webhook_endpoint_id: str | None = None
    telegram_channel_id: str | None = None
    twitter_account_id: str | None = None
    query_params: dict | None = None
    max_articles: int | None = None

    @field_validator("cron_expression")
    @classmethod
    def validate_cron(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                croniter(v)
            except Exception as e:
                raise ValueError(f"Invalid cron expression: {e}")
        return v


@router.get("/schedules")
async def list_schedules():
    """Get all webhook schedules."""
    schedules = await get_all_schedules()
    return {"schedules": schedules}


@router.get("/schedules/{schedule_id}")
async def get_schedule_by_id(schedule_id: str):
    """Get a single schedule by ID."""
    schedule = await get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(404, f"Schedule '{schedule_id}' not found")
    return schedule


@router.post("/schedules", status_code=201)
async def add_schedule(body: ScheduleIn):
    """Create a new webhook schedule."""
    if (
        not body.webhook_endpoint_id
        and not body.telegram_channel_id
        and not body.twitter_account_id
    ):
        raise HTTPException(
            400,
            "At least one of webhook_endpoint_id, telegram_channel_id, or twitter_account_id must be provided",
        )

    schedule_id = await create_schedule(
        name=body.name,
        cron_expression=body.cron_expression,
        webhook_endpoint_id=body.webhook_endpoint_id,
        telegram_channel_id=body.telegram_channel_id,
        twitter_account_id=body.twitter_account_id,
        query_params=body.query_params,
        max_articles=body.max_articles,
        enabled=body.enabled,
    )

    # Compute initial next_run_at
    try:
        now = datetime.now(timezone.utc)
        iter = croniter(body.cron_expression, now)
        next_run = iter.get_next(datetime)
        from storage.webhook_schedules import update_schedule_run_time

        await update_schedule_run_time(schedule_id, now, next_run)
    except Exception:
        pass  # If cron parsing fails, next_run_at stays null

    return {"id": schedule_id, "message": "Schedule created"}


@router.put("/schedules/{schedule_id}")
async def update_schedule_by_id(schedule_id: str, body: ScheduleUpdate):
    """Update an existing schedule."""
    existing = await get_schedule(schedule_id)
    if not existing:
        raise HTTPException(404, f"Schedule '{schedule_id}' not found")

    updated = await update_schedule(
        schedule_id=schedule_id,
        name=body.name,
        cron_expression=body.cron_expression,
        enabled=body.enabled,
        webhook_endpoint_id=body.webhook_endpoint_id,
        telegram_channel_id=body.telegram_channel_id,
        twitter_account_id=body.twitter_account_id,
        query_params=body.query_params,
        max_articles=body.max_articles,
    )

    if not updated:
        raise HTTPException(400, "No fields to update")

    # Recompute next_run_at if cron changed
    if body.cron_expression:
        try:
            now = datetime.now(timezone.utc)
            iter = croniter(body.cron_expression, now)
            next_run = iter.get_next(datetime)
            from storage.webhook_schedules import update_schedule_run_time

            await update_schedule_run_time(schedule_id, now, next_run)
        except Exception:
            pass

    return {"message": "Schedule updated"}


@router.delete("/schedules/{schedule_id}")
async def delete_schedule_by_id(schedule_id: str):
    """Delete a schedule."""
    deleted = await delete_schedule(schedule_id)
    if not deleted:
        raise HTTPException(404, f"Schedule '{schedule_id}' not found")
    return {"message": "Schedule deleted"}
