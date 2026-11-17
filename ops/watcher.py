"""Watcher jobs: fetch → diff → classify → deliver (spec section 4.8)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx

from core.auth import AuthRequired
from core.config import Settings
from core.db import DB
from core.models import Item
from ops.classify import classify
from ops.diff import Change, detect
from ops import tg
from ops.format import alert_data, alert_summary, alert_text
from ops.policy import decide, quiet_now
from ops.tg import Html

log = logging.getLogger(__name__)
SOURCE_NAMES = {"brightspace": "POLITEMall", "teams": "Teams", "group": "Class group", "outlook": "Outlook email"}
MAX_BATCH_BUTTON_ROWS = 8


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Services:
    settings: Settings
    db: DB
    sessions: Any
    bs: Any
    notifier: Any
    teams: Any = None
    group: Any = None
    clock: Callable[[], datetime] = field(default=_utcnow)
    extra: dict = field(default_factory=dict)


def buttons_for(change: Change) -> list[list[tuple[str, str]]]:
    item = change.item
    row: list[tuple[str, str]] = []
    if item.kind == "content" and item.payload.get("type") == "File":
        row.append(("Download", f"dl:{item.payload['ou']}:{item.payload['topic_id']}"))
    if item.kind == "assignment":
        row.append(("Do it", f"doit:{item.stable_id}"))
    rows = [row] if row else []
    if item.course_code and item.kind not in ("content",):
        rows.append([("Mute course", f"mute:{item.course_code}")])
    return rows


async def deliver(ctx: Services, changes: list[Change]) -> None:
    """Queued entries keep the rendered card ("text", HTML, with "html": true) and the facts ("data") so a batch can
    be grouped. Entries written by older versions have only plain "text"; they are still sent (escaped)."""
    now = ctx.clock()
    tz = ctx.settings.tz
    names = ctx.db.get_json_setting("course_names", {})
    base = getattr(ctx.settings, "politemall_base", "") or ""
    batch = ctx.db.get_json_setting("batch_queue", [])
    briefing = ctx.db.get_json_setting("briefing_queue", [])
    retry = ctx.db.get_json_setting("retry_queue", [])
    try:
        for ch in changes:
            level = classify(ch)
            decision = decide(level, ch.item.course_code, ctx.db, now)
            text = alert_text(ch, level, tz, now=now, course_names=names, base=base)
            entry = {"text": str(text), "html": True, "buttons": buttons_for(ch) or None,
                     "data": alert_data(ch, level, names, base)}
            if decision == "send":
                try:
                    await ctx.notifier.send(text, entry["buttons"])
                except Exception as e:  # noqa: BLE001 - never lose an urgent alert to a Telegram hiccup
                    log.warning("alert send failed, will retry: %s", e)
                    retry.append(entry)
            elif decision == "batch":
                batch.append(entry)
            elif decision == "briefing":
                briefing.append({"text": alert_summary(ch, level, tz, now, names), "at": now.isoformat()})
    finally:
        ctx.db.set_json_setting("batch_queue", batch)
        ctx.db.set_json_setting("briefing_queue", briefing)
        ctx.db.set_json_setting("retry_queue", retry)


def entry_text(entry: dict) -> str:
    """A queued entry as something Notifier.send understands: Html for new entries, plain str for old ones."""
    text = str(entry.get("text") or "")
    return Html(text) if entry.get("html") else text


async def flush_retry(ctx: Services) -> None:
    queue = ctx.db.get_json_setting("retry_queue", [])
    if not queue or ctx.db.get_setting("paused") == "1":
        return
    left = []
    for entry in queue:
        try:
            await ctx.notifier.send(entry_text(entry), entry.get("buttons"))
        except Exception as e:  # noqa: BLE001
            log.warning("retry send failed: %s", e)
            left.append(entry)
    ctx.db.set_json_setting("retry_queue", left)


def _file_section(course: str, entries: list[dict]) -> Html:
    """📄 Investments · 10 new files, then each module once with its items as bullets."""
    files = all((e["data"].get("type") or "") == "File" for e in entries)
    n = len(entries)
    noun = ("new file" if files else "new item") + ("" if n == 1 else "s")
    head = f"{course} · {n} {noun}" if course else f"{n} {noun}"
    by_module: dict[str, list[dict]] = {}
    for e in entries:
        by_module.setdefault(e["data"].get("module") or "", []).append(e)
    lasts = [m.split(" › ")[-1] for m in by_module]
    out = []
    for module, items in by_module.items():
        last = module.split(" › ")[-1]
        label = last if lasts.count(last) == 1 else module
        rows = [tg.Html("• " + tg.link(tg.tidy_title(e["data"].get("title")), e["data"].get("url"))) for e in items]
        out.append(tg.lines(tg.i(label) if label else "", *rows))
    return tg.lines(tg.card("📄", head), tg.blocks(*out))  # a blank line between modules


def batch_text(queue: list[dict]) -> Html:
    """The hourly "Updates" message. New files are grouped by course, then module; everything else (and old
    text-only entries) gets its own compact section."""
    groups: dict[str, list[dict]] = {}
    sections: list[str | Html] = []  # a course key (str) stands for that course's grouped files
    for e in queue:
        d = e.get("data") if e.get("html") else None
        if d and d.get("kind") == "content" and d.get("change") == "new":
            key = d.get("course") or ""
            if key not in groups:
                groups[key] = []
                sections.append(key)
            groups[key].append(e)
        else:
            text = entry_text(e)
            sections.append(text if isinstance(text, Html) else tg.to_html(text))
    out = [s if isinstance(s, Html) else _file_section(s, groups[s]) for s in sections]
    if len(out) > 1:
        out.insert(0, tg.b(f"Updates · {len(queue)} new"))
    return tg.blocks(*out)


def batch_buttons(queue: list[dict]) -> list[list[tuple[str, str]]]:
    """At most MAX_BATCH_BUTTON_ROWS rows, Mute rows last. Download buttons are named after their file (ten
    "Download" buttons in a row would be useless). Every file title is also a link to it in POLITEMall."""
    rows: list[tuple] = []
    for q in queue:
        d = q.get("data") or {}
        for row in q.get("buttons") or []:
            row = [tuple(b) for b in row]
            if d.get("title") and len(row) == 1 and row[0][0] == "Download":
                title = tg.tidy_title(d["title"])
                row = [(f"⬇️ {title[:30] + '…' if len(title) > 31 else title}", row[0][1])]
            rows.append(tuple(row))
    rows = sorted(dict.fromkeys(rows), key=lambda r: r[0][1].startswith("mute:"))  # Mute rows last
    return [list(r) for r in rows][:MAX_BATCH_BUTTON_ROWS]


async def flush_batch(ctx: Services) -> None:
    await flush_retry(ctx)
    if ctx.db.get_setting("paused") == "1" or quiet_now(ctx.db, ctx.clock(), ctx.settings.tz):
        return
    queue = ctx.db.get_json_setting("batch_queue", [])
    if not queue:
        return
    rows = batch_buttons(queue)
    try:
        await ctx.notifier.send(batch_text(queue), rows or None)
    except Exception as e:  # noqa: BLE001 - keep the queue for the next hour
        log.warning("batch send failed: %s", e)
        return
    # re-read so items queued while we were sending are kept
    current = ctx.db.get_json_setting("batch_queue", [])
    ctx.db.set_json_setting("batch_queue", current[len(queue):])


BROWSER_SITES = ("brightspace", "teams", "outlook")


async def _renewed_quietly(ctx: Services, source: str) -> bool:
    sessions = ctx.sessions
    if source not in BROWSER_SITES or sessions is None or not hasattr(sessions, "refresh"):
        return False
    try:
        return bool(await sessions.refresh(source))
    except Exception:  # noqa: BLE001
        log.warning("quiet renewal of %s crashed", source, exc_info=True)
        return False


async def _keep_cookies(ctx: Services, source: str) -> None:
    """Keep the backup copy of the session cookies as fresh as the live ones."""
    save = getattr(ctx.sessions, "save_cookies", None)
    if source in BROWSER_SITES and save is not None:
        try:
            await save(source)
        except Exception:  # noqa: BLE001
            log.debug("cookie backup failed", exc_info=True)


async def run_source(ctx: Services, source: str, collect: Callable[[], Awaitable[list[Item]]],
                     on_items: Callable[[list[Item]], None] | None = None) -> list[Change]:
    """Run one sweep with auth handling and failure accounting."""
    db, now = ctx.db, ctx.clock()
    name = SOURCE_NAMES.get(source, source)
    prev = db.conn.execute("select consecutive_failures from source_health where source=?", (source,)).fetchone()
    prev_failures = prev[0] if prev else 0
    items = None
    for attempt in (0, 1):
        try:
            items = await collect()
            break
        except AuthRequired as e:
            # A slow redirect or a short outage looks the same as a real logout, so try to renew the session quietly
            # first (browser profile + Microsoft's stay-signed-in cookies, no password, no MFA) and only then ask.
            if attempt == 0 and await _renewed_quietly(ctx, source):
                continue
            if db.get_setting(f"auth_prompted:{source}") != "1":
                reason = str(e).strip()
                why = tg.i(f"Reason: {reason[:160]}") if reason and reason != source else ""
                await ctx.notifier.send(tg.card("⚠️", f"{name} session expired",
                                                "Alerts for it are paused until you log in.", why),
                                        [[("Log in now", f"login:retry:{source}")]])
                db.set_setting(f"auth_prompted:{source}", "1")
            return []
        except Exception as e:  # noqa: BLE001
            log.exception("%s sweep failed", source)
            n = db.record_health(source, False, now)
            if n == 3:
                await ctx.notifier.send(tg.card("⚠️", f"{name} checks failing",
                                                f"3 in a row. {type(e).__name__}: {e}"[:400]))
            return []
    await _keep_cookies(ctx, source)
    db.record_health(source, True, now)
    db.set_setting(f"auth_prompted:{source}", "0")
    if prev_failures >= 3:
        await ctx.notifier.send(tg.card("✅", f"{name} is back to normal"))
    if on_items:
        on_items(items)
    fresh: dict[str, int] = {}
    changes = detect(db, source, items, now, fresh, ctx.extra.pop(f"sweep_courses:{source}", None))
    names = db.get_json_setting("course_names", {})
    for code, n in fresh.items():  # a course seen for the first time is indexed silently: one line instead of a flood
        try:
            await ctx.notifier.send(tg.card("📚", f"New course added: {tg.course(code, names)}",
                                            f"{n} items indexed quietly (no alert for each)."))
        except Exception as e:  # noqa: BLE001
            log.warning("new-course note failed: %s", e)
    await deliver(ctx, changes)
    return changes


def _upsert_due_dates(db: DB, items: list[Item]) -> None:
    for it in items:
        if it.kind != "assignment":
            continue
        due = it.payload.get("due_at") or it.payload.get("end_at")
        # A moved deadline gets a fresh set of reminders and is re-checked for submission.
        db.conn.execute(
            """insert into due_dates(item_key, due_at) values (?,?)
               on conflict(item_key) do update set
                 reminders_sent_json = case when due_dates.due_at is excluded.due_at
                                            then due_dates.reminders_sent_json else '[]' end,
                 submitted = case when due_dates.due_at is excluded.due_at then due_dates.submitted else 0 end,
                 user_confirmed = case when due_dates.due_at is excluded.due_at then due_dates.user_confirmed else 0 end,
                 snooze_until = case when due_dates.due_at is excluded.due_at then due_dates.snooze_until else null end,
                 confirm_note_sent = case when due_dates.due_at is excluded.due_at
                                          then due_dates.confirm_note_sent else 0 end,
                 missed_sent = case when due_dates.due_at is excluded.due_at then due_dates.missed_sent else 0 end,
                 due_at = excluded.due_at""",
            (it.stable_id, due),
        )


SUBMITTED_CHECK_WINDOW_DAYS = 7


async def _refresh_submitted(ctx: Services, items: list[Item]) -> None:
    from datetime import timedelta

    from ops.format import parse_iso

    now = ctx.clock()
    horizon = now + timedelta(days=SUBMITTED_CHECK_WINDOW_DAYS)
    for it in items:
        if it.kind != "assignment":
            continue
        due = parse_iso(it.payload.get("due_at") or it.payload.get("end_at"))
        if not due or not (now - timedelta(days=1) <= due <= horizon):
            continue
        row = ctx.db.conn.execute("select submitted from due_dates where item_key=?", (it.stable_id,)).fetchone()
        if row and row[0]:
            continue
        try:
            if await ctx.bs.submitted(it.payload["ou"], it.payload["folder_id"]):
                # found long before the deadline: no "Submission confirmed" note later (it is for the final 24 h)
                ctx.db.conn.execute("update due_dates set submitted=1, confirm_note_sent=? where item_key=?",
                                    (int(due - now > timedelta(hours=24)), it.stable_id))
        except AuthRequired:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("submission check failed for %s: %s", it.stable_id, e)


async def sweep_brightspace(ctx: Services) -> list[Change]:
    async def collect() -> list[Item]:
        items: list[Item] = []
        calls = failures = 0
        last_error: Exception | None = None
        courses = await ctx.bs.courses()
        complete: set[int] = set()  # courses whose fetches ALL worked: only those may be baselined as "new"
        ctx.extra["sweep_courses:brightspace"] = complete
        for c in courses:
            before = failures
            for fetch in (ctx.bs.assignment_items, ctx.bs.announcements, ctx.bs.content_items, ctx.bs.grades):
                calls += 1
                try:
                    items += await fetch(c.ou, c.code)
                except AuthRequired:
                    raise  # (a 403 only gets here as AuthRequired when whoami also fails)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 403:  # tool switched off in this course: a successful empty read
                        log.info("%s %s is switched off (403)", c.code, getattr(fetch, "__name__", fetch))
                        continue
                    failures += 1
                    last_error = e
                    log.warning("%s %s failed: %s", c.code, getattr(fetch, "__name__", fetch), e)
                except Exception as e:  # noqa: BLE001 - one disabled tool must not stop the sweep
                    failures += 1
                    last_error = e
                    log.warning("%s %s failed: %s", c.code, getattr(fetch, "__name__", fetch), e)
            if failures == before:
                complete.add(c.ou)
        if calls and failures == calls and last_error:
            raise last_error
        _upsert_due_dates(ctx.db, items)
        await _refresh_submitted(ctx, items)
        return items

    return await run_source(ctx, "brightspace", collect)


TEAMS_LOOKBACK_HOURS = 6


async def sweep_teams(ctx: Services) -> list[Change]:
    from datetime import timedelta

    if not ctx.teams:
        return []
    since = ctx.clock() - timedelta(hours=TEAMS_LOOKBACK_HOURS)
    return await run_source(ctx, "teams", lambda: ctx.teams.recent(since))
