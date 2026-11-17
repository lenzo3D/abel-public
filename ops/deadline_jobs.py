"""Scheduled deadline work: Brightspace → deadlines → Google Calendar; library refresh + extraction."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from core import hostos, project_tasks, usagecap
from core.deadlines import DeadlineStore, display_moment, local_date
from core.gcal import CalendarAuthRequired, GCal
from ops import project_tasks as project_tasks_ops
from ops import tg
from ops.calsync import sync
from ops.deadline_sources import _course_names, extract_chats, refresh_confirmed
from ops.scan import full_scan

log = logging.getLogger(__name__)
_sync_lock = asyncio.Lock()
_scan_lock = asyncio.Lock()


def get_gcal(ctx):
    if ctx.extra.get("gcal") is not None:
        return ctx.extra["gcal"]
    token = ctx.settings.state_dir / "google_token.json"
    if not token.exists():
        return None
    ctx.extra["gcal"] = GCal(token_path=token, client_path=ctx.settings.state_dir / "google_client.json")
    return ctx.extra["gcal"]


async def calendar_sync(ctx):
    async with _sync_lock:
        return await _calendar_sync(ctx)


async def _calendar_sync(ctx):
    try:  # your own project-group tasks and the groups' meetings become tentative deadlines first (local, no network)
        project_tasks.sync_deadlines(ctx.settings, ctx.db, ctx.clock())
    except Exception:  # noqa: BLE001 - never let this stop the calendar
        log.warning("project task calendar mirror failed", exc_info=True)
    gcal = get_gcal(ctx)
    if gcal is None:
        return None
    try:
        rep = await sync(DeadlineStore(ctx.db), gcal, await _course_names(ctx), ctx.settings.tz)
    except CalendarAuthRequired as e:
        if ctx.db.get_setting("gcal_warned") != "1":
            await ctx.notifier.send(tg.card("⚠️", "Google Calendar needs reconnecting", str(e)[:300],
                                            tg.Html("Run: " + tg.code(hostos.python_hint('-m core.gcal login')))))
            ctx.db.set_setting("gcal_warned", "1")
        ctx.extra.pop("gcal", None)
        return None
    ctx.db.set_setting("gcal_warned", "0")
    if rep.inserted or rep.patched or rep.deleted:
        log.info("calendar sync: %s", rep)
    return rep


async def confirmed_job(ctx) -> None:
    """Every 15 min: Brightspace due dates → deadlines → calendar."""
    await refresh_confirmed(ctx)
    await calendar_sync(ctx)


async def extract_job(ctx) -> dict | None:
    """Hourly: re-read changed course material, extract tentative dates from it and from chats, sync.
    Returns None if a scan is already running (never two at once)."""
    if _scan_lock.locked():
        return None
    async with _scan_lock:
        stats = await full_scan(ctx, extract=True)
        stats["deadlines"] += await extract_chats(ctx)
        try:
            await project_tasks_ops.extract_all(ctx)
        except Exception:  # noqa: BLE001 - the project task list must never stop the date scan
            log.warning("project task extraction failed", exc_info=True)
        await refresh_confirmed(ctx)
    await calendar_sync(ctx)
    return stats


async def scan_command(ctx):
    stats = await extract_job(ctx)
    if stats is None:
        return tg.card("🔎", "A scan is already running", "I'll post the results when it's done.")
    total = len(DeadlineStore(ctx.db).active())
    cal = "synced to Google Calendar" if get_gcal(ctx) else "Google Calendar not connected yet"
    errors = f" · {stats['errors']} errors" if stats["errors"] else ""
    held = ("⏸️ Date extraction is paused to save usage; it picks up where it left off when the period resets "
            "(or when you /usage resume).") if usagecap.paused(ctx) else ""
    return tg.blocks(
        tg.card("🔎", "Scan done",
                f"{stats['courses']} courses · {stats['documents']} documents · {stats['announcements']} announcements"
                f" · {stats['assignments']} assignment briefs{errors}",
                tg.Html(f"{tg.b(str(stats['deadlines']))} new tentative date(s) · {total} deadlines tracked ({cal})")),
        held, "Ask me anything, for example \"when are all my assignments due?\"")


async def deadlines_text(ctx, days: int = 14):
    """/deadlines: grouped by day, with the short course name and the submission state."""
    tz = ctx.settings.tz
    items = DeadlineStore(ctx.db).upcoming(ctx.clock(), days, tz)
    if not items:
        return tg.card("🗓️", f"No deadlines in the next {days} days")
    names = await _course_names(ctx)
    days_: dict[str, list] = {}
    for d in items:
        tags = []
        if d.confidence != "confirmed":
            tags.append(tg.i("tentative"))
        row = ctx.db.conn.execute("select submitted, user_confirmed from due_dates where item_key=?", (d.key,)).fetchone()
        if row and row["submitted"]:
            tags.append(tg.esc("✅ submitted"))
        elif row and row["user_confirmed"]:
            tags.append(tg.i("you say submitted; POLITEMall has not confirmed it"))
        if d.all_day:
            day, time_ = local_date(d, tz).strftime("%a %d %b"), "all day"
        else:
            at = datetime.fromisoformat(display_moment(d)).astimezone(tz)
            day, time_ = at.strftime("%a %d %b"), at.strftime("%H:%M")
        who = "Project" if d.source == "project" else tg.course(d.course_code, names) if d.course_code else ""
        parts = [tg.esc(time_)] + ([tg.esc(who)] if who else []) + [tg.esc(tg.tidy_title(d.title))] + tags
        days_.setdefault(day, []).append(tg.Html("• " + " · ".join(parts)))
    sections = [tg.lines(tg.b(day), *rows) for day, rows in days_.items()]
    return tg.blocks(tg.card("🗓️", f"Next {days} days"), *sections)
