"""Telegram alert cards (HTML, built with ops/tg.py) and the shared date helpers."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from core.text import html_to_text  # noqa: F401  (re-exported)
from ops import tg
from ops.diff import Change
from ops.tg import Html

KIND_LABEL = {
    "assignment": "assignment", "announcement": "announcement", "content": "new material",
    "grade": "grade released", "teams_post": "Teams post", "teams_dm": "Teams message",
    "teams_mention": "Teams mention", "group_action": "class chat", "email": "email",
}
ICON = {"assignment": "📌", "content": "📄", "announcement": "📢", "teams_post": "💬", "teams_dm": "💬",
        "teams_mention": "💬", "group_action": "💬", "email": "✉️", "grade": "🎓", "due_change": "🗓️"}


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_dt(dt_utc: datetime, tz) -> str:
    """Fri 13 Nov, 14:00 (local time)."""
    return tg.when(dt_utc, tz)


def countdown(due: datetime, now: datetime) -> str:
    return tg.until(due, now)


def _course_label(code: str | None, names: dict[str, str] | None) -> str:
    return tg.course(code, names)


BODY_LIMIT = 1500  # stored bodies are already plain text (capped at 1500-2000 on fetch); don't cut them harder here


def _clip(text: str, limit: int = BODY_LIMIT) -> str:
    text = re.sub(r"[ \t]+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip() + "…"


def content_url(item, base: str = "") -> str | None:
    """Where a new file or page opens in POLITEMall (the D2L content viewer)."""
    p = item.payload
    if base and p.get("ou") is not None and p.get("topic_id") is not None:
        return f"{base.rstrip('/')}/d2l/le/content/{p['ou']}/viewContent/{p['topic_id']}/View"
    url = p.get("url") or ""
    return url if url.startswith(("http://", "https://")) else None


def alert_data(change: Change, level: str, course_names: dict[str, str] | None = None, base: str = "") -> dict:
    """The facts of an alert, kept with queued entries so a batch can be grouped (by course, then module)."""
    item, p = change.item, change.item.payload
    return {"kind": item.kind, "change": change.kind, "level": level, "code": item.course_code,
            "course": _course_label(item.course_code, course_names), "title": item.title,
            "module": p.get("module") or "", "type": p.get("type") or "",
            "url": content_url(item, base) if item.kind == "content" else None}


def alert_summary(change: Change, level: str, tz, now: datetime | None = None,
                  course_names: dict[str, str] | None = None) -> str:
    """One plain line for the morning briefing's input (the model reads it; no HTML)."""
    item, p = change.item, change.item.payload
    course = _course_label(item.course_code, course_names)
    if item.kind == "email":
        who = p.get("sender") or p.get("address") or "unknown sender"
        return f"Email from {who}: {item.title}"
    what = KIND_LABEL.get(item.kind, item.kind)
    if change.kind == "changed":
        what = f"updated {what}"
    where = f" ({p['module']})" if item.kind == "content" and p.get("module") else ""
    return f"{course + ' · ' if course else ''}{what.capitalize()}: {item.title}{where}"


def _assignment(change: Change, tz, now, course: str) -> Html:
    item = change.item
    due = parse_iso(item.payload.get("due_at")) or parse_iso(item.payload.get("end_at"))
    title = tg.tidy_title(item.title)
    if "due_at" in change.changed_fields:
        old, new = (parse_iso(v) for v in change.changed_fields["due_at"])
        head = f"{course} · Due date changed" if course else "Due date changed"
        moved = f"Was {fmt_dt(old, tz) if old else 'none'} → now {fmt_dt(new, tz) if new else 'none'}"
        return tg.card(ICON["due_change"], head, title, tg.b(moved))
    what = "Updated assignment" if change.kind == "changed" else "New assignment"
    if due:
        left = f" · {tg.until(due, now, tz)}" if now else ""
        when = f"🗓️ Due {fmt_dt(due, tz)}{left}"
    else:
        when = "No due date set"
    return tg.card(ICON["assignment"], f"{course} · {what}" if course else what, tg.b(title), when)


def alert_text(change: Change, level: str, tz, now: datetime | None = None,
               course_names: dict[str, str] | None = None, base: str = "") -> Html:
    """The Telegram card for one change. All outside text is escaped (tg.esc / tg.body)."""
    item, p = change.item, change.item.payload
    course = _course_label(item.course_code, course_names)
    urgent = level == "red"

    if item.kind == "email":
        who = p.get("sender") or p.get("address") or "unknown sender"
        head = f"{'Important email' if urgent else 'Email'} · {who}"
        top = tg.card(ICON["email"], head, tg.b(item.title or "(no subject)"))
        link = tg.link("Open in Outlook", p.get("link")) if p.get("link") else ""
        return tg.blocks(top, tg.body(_clip(p.get("preview") or "", 400)), link)

    if item.kind == "assignment":
        return _assignment(change, tz, now, course)

    if item.kind == "group_action":
        group = p.get("group") or "Class chat"
        due = p.get("due") or re.sub(r"^Deadline:\s*", "", p.get("body_text") or "").strip()
        return tg.card(ICON["group_action"], f"{group} · action needed", tg.esc(item.title),
                       f"🗓️ Due {due}" if due else "")

    if item.kind == "content":
        what = "Updated material" if change.kind == "changed" else "New material"
        title = tg.link(item.title, content_url(item, base))
        where = " · ".join(x for x in (p.get("module"), p.get("type")) if x)
        return tg.lines(tg.card(ICON["content"], f"{course} · {what}" if course else what, title),
                        tg.i(where) if where else "")

    if item.kind == "grade":
        top = tg.card(ICON["grade"], f"{course} · Grade released" if course else "Grade released", tg.esc(item.title))
        if p.get("value") is not None:
            out_of = p.get("out_of")
            top = tg.lines(top, tg.Html("Grade: " + tg.b(f"{p['value']}" + (f" / {out_of}" if out_of else ""))))
        return top

    if item.kind.startswith("teams_"):
        label = KIND_LABEL.get(item.kind, "Teams")
        top = tg.card(ICON[item.kind] if item.kind in ICON else "💬", f"{label} · {item.title}")
        return tg.blocks(top, tg.body(_clip(p.get("body_text") or "")))

    if item.kind == "announcement":
        what = "Updated announcement" if change.kind == "changed" else "Announcement"
        if urgent:
            what += " · important"
        top = tg.card(ICON["announcement"], f"{course} · {what}" if course else what, tg.b(item.title))
        return tg.blocks(top, tg.body(_clip(p.get("body_text") or "")))

    what = KIND_LABEL.get(item.kind, item.kind).capitalize()
    top = tg.card("🔔", f"{course} · {what}" if course else what, tg.esc(item.title))
    return tg.blocks(top, tg.body(_clip(p.get("body_text") or "")))
