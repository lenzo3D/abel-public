"""Telegram HTML for everything Abel sends to the user (parse_mode="HTML").

Two kinds of text reach `Notifier.send`:

- `Html`: built here from the small helpers below. Every piece of outside text (course pages, chats, email, file
  names) goes in through `esc()` or a helper that escapes, so a "<b>" or "</blockquote>" inside a Teams post shows
  literally. Only the tags written in this module ever reach Telegram.
- a plain `str` (the model's replies, short status lines): `to_html()` escapes all of it, then turns the light
  Markdown the model writes (**bold**, *italic*, [text](url), "- " bullets, `code`) into tags. Raw "<" or "&" never
  pass through.

`render()` splits a message for Telegram's 4096 limit without ever cutting inside a tag, and `to_plain()` is the
fallback when Telegram still refuses the HTML ("can't parse entities"), so an alert is never lost.

Group replies (ops/group_reply.py) post into groups as plain text and keep using ops.textfmt.plain.
"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime
from urllib.parse import urlsplit

from ops.telegram_out import TEXT_LIMIT, utf16_len


class Html(str):
    """Text that is already safe Telegram HTML. Plain `str` is converted (escaped) on the way out."""

    __slots__ = ()


def esc(text) -> Html:
    """Escape outside text for Telegram HTML. An `Html` value is already safe and is returned unchanged."""
    if isinstance(text, Html):
        return text
    return Html(_html.escape("" if text is None else str(text), quote=False))


def _attr(value: str) -> str:
    return _html.escape(value, quote=True)


def b(text) -> Html:
    return Html(f"<b>{esc(text)}</b>")


def i(text) -> Html:
    return Html(f"<i>{esc(text)}</i>")


def code(text) -> Html:
    return Html(f"<code>{esc(text)}</code>")


def link(label, url: str | None) -> Html:
    """A tappable label. Anything but an http(s) URL is shown as the bare label (no javascript:, tg:, file: links)."""
    url = (url or "").strip()
    if not re.match(r"(?i)^https?://[^\s<>\"']+$", url):
        return esc(label)
    return Html(f'<a href="{_attr(url)}">{esc(label)}</a>')


def quote(text, expandable: bool = True) -> Html:
    """A block quote; expandable ones show a few lines and open on tap."""
    body = esc(text).strip("\n")
    if not body:
        return Html("")
    return Html(f"<blockquote{' expandable' if expandable else ''}>{body}</blockquote>")


def bullets(items, mark: str = "•") -> Html:
    return Html("\n".join(f"{mark} {esc(x)}" for x in items if str(x).strip()))


def lines(*parts) -> Html:
    """One per line; empty parts are left out. Each part is escaped unless it is already Html."""
    return Html("\n".join(esc(p) for p in parts if p))


def blocks(*parts) -> Html:
    """Paragraphs separated by a blank line (the places `render` may split a long message)."""
    return Html("\n\n".join(esc(p).strip("\n") for p in parts if p and str(p).strip()))


def card(icon: str, title, *body) -> Html:
    """The house style: one leading icon, a bold title line, then short lines."""
    head = Html(f"{icon} {b(title)}" if icon else b(title))
    return lines(head, *body)


# ---------------------------------------------------------------- Markdown (model output) to HTML

_PH = "\x00"
_FENCE = re.compile(r"^```[\w+-]*[ \t]*\n(.*?)(?:\n```[ \t]*$|\Z)", re.M | re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BARE_URL = re.compile(r"https?://[^\s<>\"'`]+")
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.*?)[ \t]*#*[ \t]*$", re.M)
_RULE = re.compile(r"^[ \t]*([-*_])([ \t]*\1){2,}[ \t]*$\n?", re.M)
_BULLET = re.compile(r"^([ \t]*)[*+-][ \t]+", re.M)
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.S)
_ITALIC_STAR = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])")
_ITALIC_UNDER = re.compile(r"(?<![\w_])_(?=[^\s_])([^_\n]+?)(?<=[^\s_])_(?![\w_])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")


def _trim_url(url: str) -> str:
    while url and url[-1] in ".,;:!?'\"":
        url = url[:-1]
    while url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1]
    return url


def to_html(text: str | None) -> Html:
    """Plain or lightly-Markdown text (usually the model's reply) as safe Telegram HTML. Emoji are kept."""
    if isinstance(text, Html):
        return text
    text = (text or "").replace(_PH, "").replace("\r\n", "\n")
    saved: list[str] = []

    def keep(fragment: str) -> str:
        saved.append(fragment)
        return f"{_PH}{len(saved) - 1}{_PH}"

    # 1. pieces whose inside must not be touched: code, links, bare URLs (an "_" in a URL is not italics)
    text = _FENCE.sub(lambda m: keep(f"<pre>{esc(m.group(1))}</pre>"), text)
    text = _INLINE_CODE.sub(lambda m: keep(str(code(m.group(1)))), text)
    text = _MD_LINK.sub(lambda m: keep(str(link(m.group(1), m.group(2)))), text)

    def bare(m: re.Match) -> str:
        url = _trim_url(m.group(0))
        return keep(str(esc(url))) + m.group(0)[len(url):]

    text = _BARE_URL.sub(bare, text)
    # 2. line structure
    text = _RULE.sub("", text)
    text = _HEADING.sub(lambda m: "**" + m.group(1) + "**" if m.group(1) else "", text)
    text = _BULLET.sub(lambda m: m.group(1) + "• ", text)
    # 3. escape everything else, then emphasis (the markers survive escaping unchanged)
    text = esc(text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(2)}</b>", text)
    text = _ITALIC_STAR.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _ITALIC_UNDER.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", text)
    text = re.sub(f"{_PH}(\\d+){_PH}", lambda m: saved[int(m.group(1))], text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip("\n")
    return Html(text)


def md(text: str | None) -> Html:
    """Alias used where a builder embeds model output inside its own HTML."""
    return to_html(text)


# ---------------------------------------------------------------- back to plain text (fallback)

_A = re.compile(r'<a href="([^"]*)">(.*?)</a>', re.S)
_TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def to_plain(text: str) -> str:
    """Tags stripped and entities decoded: what is sent when Telegram refuses the HTML. Links keep their address."""
    def anchor(m: re.Match) -> str:
        url, label = _html.unescape(m.group(1)), _TAG.sub("", m.group(2))
        return label if _html.unescape(label) == url else f"{label} ({url})"

    text = _A.sub(anchor, str(text))
    text = re.sub(r"</blockquote>", "\n", text)
    return _html.unescape(_TAG.sub("", text)).strip("\n")


# ---------------------------------------------------------------- splitting without cutting a tag

_TAG_ANY = re.compile(r"<(/?)([a-zA-Z-]+)[^>]*>")


def _top_split(text: str, sep: str) -> list[str]:
    """Split on `sep` only where no tag is open."""
    out, start, depth, pos = [], 0, 0, 0
    tags = [(m.start(), m.end(), m.group(1) == "/") for m in _TAG_ANY.finditer(text)]
    t = 0
    while pos < len(text):
        if t < len(tags) and tags[t][0] == pos:
            depth += -1 if tags[t][2] else 1
            pos = tags[t][1]
            t += 1
            continue
        if depth <= 0 and text.startswith(sep, pos):
            out.append(text[start:pos])
            pos += len(sep)
            start = pos
            continue
        pos += 1
    out.append(text[start:])
    return out


def _hard(block: str, limit: int) -> list[str]:
    """A block too long to keep whole: plain text, escaped again, cut by length."""
    out, cur, n = [], [], 0
    for ch in to_plain(block):
        piece = str(esc(ch))
        w = utf16_len(piece)
        if n + w > limit:
            out.append("".join(cur))
            cur, n = [], 0
        cur.append(piece)
        n += w
    if cur:
        out.append("".join(cur))
    return out


def _pack(pieces: list[str], sep: str, limit: int, too_long) -> list[str]:
    out: list[str] = []
    cur = ""
    for p in pieces:
        if utf16_len(p) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.extend(too_long(p))
            continue
        cand = p if not cur else cur + sep + p
        if utf16_len(cand) <= limit:
            cur = cand
        else:
            out.append(cur)
            cur = p
    if cur:
        out.append(cur)
    return out


def split_html(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Parts of at most `limit` UTF-16 units (measured on the HTML), split at blank lines, then lines, outside tags."""
    if utf16_len(text) <= limit:
        return [text]

    def by_line(block: str) -> list[str]:
        parts = _top_split(block, "\n")
        if len(parts) == 1:
            return _hard(block, limit)
        return _pack(parts, "\n", limit, lambda p: _hard(p, limit))

    return [p for p in _pack(_top_split(text, "\n\n"), "\n\n", limit, by_line) if p.strip()]


def render(text) -> list[str]:
    """What Notifier sends: the message as HTML parts."""
    h = text if isinstance(text, Html) else to_html(text)
    return split_html(str(h)) or [""]


# ---------------------------------------------------------------- shared wording

def tidy_code(code: str | None) -> str:
    """26S2-731CMBK_013682 -> CMBK. Codes without the school decoration stay as they are (INV2101)."""
    c = re.sub(r"^\d{2}S\d-", "", code or "")
    c = re.sub(r"_\d+(-ET)?$", "", c)
    m = re.fullmatch(r"\d+([A-Za-z].*)", c)
    return (m.group(1) if m else c) or (code or "")


COURSE_NAME_MAX = 24


def course(code: str | None, names: dict | None = None) -> str:
    """The one course label used in every message: the short course name, or the tidy code when the name is long or
    unknown. Never the raw POLITEMall code."""
    if not code:
        return ""
    from ops.gcal_events import short_course

    name = (names or {}).get(code) or ""
    short = short_course(name, None) if name else ""
    if short and short != "School" and len(short) <= COURSE_NAME_MAX:
        return short
    return tidy_code(code)


def tidy_title(title: str | None) -> str:
    """'Reflection - Fireside Chat (due by 13 Nov, 2pm)' -> 'Reflection – Fireside Chat': the due date is shown anyway."""
    t = re.sub(r"\s+", " ", title or "").strip()
    t = re.sub(r"\s*\((?:due|deadline|submit)\b[^()]*\)\s*$", "", t, flags=re.I)
    return t.replace(" - ", " – ") or (title or "").strip()


def when(dt: datetime, tz) -> str:
    """Fri 13 Nov, 14:00"""
    return dt.astimezone(tz).strftime("%a %d %b, %H:%M")


def until(due: datetime, now: datetime, tz=None) -> str:
    """'in 45 min', 'in 2h 59m', 'tomorrow', 'in 3 days', 'overdue'."""
    secs = (due - now).total_seconds()
    if secs < 0:
        return "overdue"
    mins = int(secs // 60)
    if mins < 60:
        return f"in {max(mins, 1)} min"
    if secs < 86400:
        h, m = divmod(mins, 60)
        return f"in {h}h {m}m" if m else f"in {h}h"
    if tz is not None:
        days = (due.astimezone(tz).date() - now.astimezone(tz).date()).days
    else:
        days = int(secs // 86400)
    if days <= 1:
        return "tomorrow"
    return f"in {days} days"


# ---------------------------------------------------------------- long bodies (posts, announcements, email)

SUMMARY_CHARS = 200
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[A-Z0-9])")
_LINK_NAMES = [
    (r"(^|\.)forms\.(office|microsoft)\.com$", "Microsoft Form"),
    (r"(^|\.)teams\.(microsoft|live)\.com$", "Teams link"),
    (r"(^|\.)outlook\.(office|office365|live)\.com$", "Outlook link"),
    (r"(^|\.)sharepoint\.com$", "SharePoint link"),
    (r"(^|\.)(1drv\.ms|onedrive\.live\.com)$", "OneDrive link"),
    (r"^forms\.gle$", "Google Form"),
    (r"(^|\.)docs\.google\.com$", "Google Docs link"),
    (r"(^|\.)drive\.google\.com$", "Google Drive link"),
    (r"(^|\.)(zoom\.us|zoom\.com)$", "Zoom link"),
    (r"(^|\.)(youtube\.com|youtu\.be)$", "YouTube video"),
    (r"(politemall|brightspace|d2l)|(^|\.)polite\.edu\.sg$", "POLITEMall link"),
]


def url_label(url: str) -> str:
    """A short name for a link: 'POLITEMall link', 'Microsoft Form', or the bare domain ('8prasia.com')."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    if url.lower().startswith("https://docs.google.com/forms"):
        return "Google Form"
    for pattern, name in _LINK_NAMES:
        if re.search(pattern, host):
            return name
    host = re.sub(r"^www\d?\.", "", host)
    return host or "link"


def linkify(text: str) -> Html:
    """Escape text, turning raw URLs into short labelled links."""
    out, last = [], 0
    for m in _BARE_URL.finditer(text or ""):
        url = _trim_url(m.group(0))
        out.append(esc(text[last:m.start()]))
        out.append(link(url_label(url), url))
        last = m.start() + len(url)
    out.append(esc((text or "")[last:]))
    return Html("".join(out))


def _tidy_body(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\xa0", " ")
    text = "\n".join(re.sub(r"[ \t\f\v]+", " ", ln).strip() for ln in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _split_summary(text: str, limit: int) -> tuple[str, str]:
    if len(text) <= limit:
        return text, ""
    first_para = text.split("\n", 1)[0]
    sentences = _SENTENCE.split(first_para)
    head = ""
    for s in sentences[:2]:
        cand = f"{head} {s}".strip()
        if len(cand) > limit:
            break
        head = cand
    if head:
        return head, text[len(head):].strip()
    # one long sentence: cut on a word boundary (never inside a URL, which has no spaces)
    cut = text[:limit].rsplit(" ", 1)[0].rstrip() if " " in text[:limit] else text[:limit]
    return cut + " …", "… " + text[len(cut):].strip()


def body(text: str | None, limit: int = SUMMARY_CHARS) -> Html:
    """The first sentence or two as the visible summary; the rest in an expandable quote. Links shortened."""
    text = _tidy_body(text or "")
    if not text:
        return Html("")
    head, rest = _split_summary(text, limit)
    if not rest:
        return linkify(head)
    return Html(f"{linkify(head)}\n<blockquote expandable>{linkify(rest)}</blockquote>")
