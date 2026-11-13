"""Scheduled BBMP collection (APScheduler, in-process; no queue or extra service).

Configuration, highest priority first:
  1. the schedule saved through the API (app_settings 'bbmp_schedule'),
  2. environment: BBMP_SCHEDULE_ENABLED=1, BBMP_SCHEDULE_CRON="0 3 * * 0" (5-field cron: here every Sunday 03:00), BBMP_SCHEDULE_TZ=Asia/Kolkata.
It is off unless enabled. A run is skipped when another collection is still running, so several workers or a slow run never overlap.
With more than one web worker, enable the schedule in only one of them (or keep it off and use an external cron to call the API).
"""
import os
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import models as m
from .db import SessionLocal

JOB_ID = "bbmp_collection"
SETTING = "bbmp_schedule"
DEFAULT_CRON = "0 3 * * 0"
DEFAULT_TZ = "Asia/Kolkata"
_scheduler: BackgroundScheduler | None = None


def env_settings():
    return {"enabled": os.getenv("BBMP_SCHEDULE_ENABLED", "0").lower() in ("1", "true", "yes"),
            "cron": os.getenv("BBMP_SCHEDULE_CRON", DEFAULT_CRON), "timezone": os.getenv("BBMP_SCHEDULE_TZ", DEFAULT_TZ)}


def get_settings(db=None):
    own = db is None
    db = db or SessionLocal()
    try:
        row = db.get(m.AppSetting, SETTING)
        s = {**env_settings(), **(row.value if row and row.value else {})}
        s["source"] = "saved" if row and row.value else "environment"
        return s
    finally:
        if own:
            db.close()


_DOW = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]


def _dow_to_names(field: str) -> str:
    """Standard cron numbers the weekdays 0/7 = Sunday ... 6 = Saturday; APScheduler numbers them 0 = Monday. Numbers are therefore
    turned into day names (which both read the same way) so '0 3 * * 0' really means Sunday."""
    if field in ("*", "?") or not any(c.isdigit() for c in field):
        return field
    days = set()
    for part in field.split(","):
        base, _, step = part.partition("/")
        if base in ("*", "?"):
            lo, hi = 0, 6
        elif "-" in base:
            a, b = base.split("-", 1)
            lo, hi = int(a), int(b)
        else:
            lo = hi = int(base)
            if step:
                hi = 6
        if not (0 <= lo <= 7 and 0 <= hi <= 7 and lo <= hi):
            raise ValueError(f"day-of-week {part!r} is out of range (0-7, where 0 and 7 are Sunday)")
        st = int(step) if step else 1
        if st < 1:
            raise ValueError(f"day-of-week step {step!r} is not valid")
        days.update(d % 7 for d in range(lo, hi + 1, st))
    return ",".join(_DOW[d] for d in sorted(days))


def build_trigger(cron: str, tz: str) -> CronTrigger:
    """Raises ValueError with a plain message for a cron expression or time zone that APScheduler cannot read."""
    parts = (cron or "").split()
    if len(parts) != 5:
        raise ValueError("The schedule must be a 5-field cron expression: minute hour day-of-month month day-of-week (for example '0 3 * * 0').")
    try:
        parts[4] = _dow_to_names(parts[4].lower())
        return CronTrigger.from_crontab(" ".join(parts), timezone=tz)
    except Exception as e:
        raise ValueError(f"Cannot read the schedule {cron!r} in time zone {tz!r}: {e}")


def run_scheduled():
    """The scheduled job: start a run unless one is in progress, then do it."""
    from .collection import start_run, execute_run, RunInProgress
    db = SessionLocal()
    try:
        try:
            run = start_run(db, "scheduled", None, {"schedule": get_settings(db)})
        except RunInProgress:
            return None
        run_id = run.id
    finally:
        db.close()
    execute_run(run_id)
    if os.getenv("WEB_CRAWL_ON_SCHEDULE", "1").lower() not in ("1", "true", "yes"):
        return run_id
    # then the live crawl of the government websites (the primary collection path)
    from .web_collection import start_web_run, execute_web_run
    db = SessionLocal()
    try:
        try:
            wrun = start_web_run(db, "scheduled", None, {"schedule": True})
        except RunInProgress:
            return run_id
        wid = wrun.id
    finally:
        db.close()
    execute_web_run(wid)
    return run_id


def apply(settings: dict | None = None):
    """(Re)configure the scheduler from the settings. Returns the next run time (or None when the schedule is off)."""
    global _scheduler
    s = settings or get_settings()
    if not s.get("enabled"):
        if _scheduler is not None and _scheduler.get_job(JOB_ID):
            _scheduler.remove_job(JOB_ID)
        return None
    trig = build_trigger(s["cron"], s["timezone"])
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone=s["timezone"])
        _scheduler.start()
    _scheduler.add_job(run_scheduled, trig, id=JOB_ID, replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600)
    job = _scheduler.get_job(JOB_ID)
    return job.next_run_time if job else None


def next_run_time():
    if _scheduler is None:
        return None
    job = _scheduler.get_job(JOB_ID)
    return job.next_run_time if job else None


def save(db, enabled: bool, cron: str, tz: str):
    build_trigger(cron, tz)  # validate before saving
    row = db.get(m.AppSetting, SETTING)
    value = {"enabled": bool(enabled), "cron": cron, "timezone": tz}
    if row is None:
        db.add(m.AppSetting(key=SETTING, value=value))
    else:
        row.value, row.updated_at = value, datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    return apply({**value})


def shutdown():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
