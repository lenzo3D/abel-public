"""Telegram HTML: helpers, Markdown conversion, safe splitting and the plain-text fallback."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from telegram.error import BadRequest

from ops import tg
from ops.bot import Notifier, reply_html
from ops.telegram_out import utf16_len
from ops.tg import Html

SGT = ZoneInfo("Asia/Singapore")


def test_helpers_escape_outside_text_and_keep_html():
    assert tg.esc("a < b & c > d") == "a &lt; b &amp; c &gt; d"
    assert tg.b("<i>x</i>") == "<b>&lt;i&gt;x&lt;/i&gt;</b>"
    assert tg.b(tg.i("x")) == "<b><i>x</i></b>"  # Html is not escaped twice
    assert tg.code("a<b") == "<code>a&lt;b</code>"
    assert tg.quote("</blockquote><b>") == "<blockquote expandable>&lt;/blockquote&gt;&lt;b&gt;</blockquote>"
    assert tg.quote("x", expandable=False) == "<blockquote>x</blockquote>"
    assert tg.bullets(["one", "<two>", " "]) == "• one\n• &lt;two&gt;"
    assert isinstance(tg.card("📄", "T", "a"), Html)


def test_links_only_for_http_and_attributes_are_quoted():
    assert tg.link("x", "https://a.sg/p?q=1&r=2") == '<a href="https://a.sg/p?q=1&amp;r=2">x</a>'
    assert tg.link("click", "javascript:alert(1)") == "click"
    assert tg.link("<b>", "tg://resolve?domain=x") == "&lt;b&gt;"
    assert tg.link("x", 'https://a.sg/"onmouseover="') == "x"  # quotes cannot appear in an href


def test_markdown_from_the_model_becomes_html():
    src = "**⏰ Due soon**\n- Quiz 2 on *Friday*\n* Read [notes](https://x.sg/a_b)\n`INV2101` and _this_"
    assert tg.to_html(src) == ('<b>⏰ Due soon</b>\n• Quiz 2 on <i>Friday</i>\n• Read <a href="https://x.sg/a_b">notes</a>'
                               "\n<code>INV2101</code> and <i>this</i>")


def test_markdown_conversion_never_passes_raw_markup_through():
    src = 'See <b>this</b> & <a href="x">that</a> </blockquote> 2 < 3'
    out = tg.to_html(src)
    assert "<b>" not in out and "<a " not in out and "</blockquote>" not in out
    assert out == 'See &lt;b&gt;this&lt;/b&gt; &amp; &lt;a href="x"&gt;that&lt;/a&gt; &lt;/blockquote&gt; 2 &lt; 3'
    assert tg.to_html("**<script>**") == "<b>&lt;script&gt;</b>"
    assert tg.to_html("[x](javascript:alert(1))") == "[x](javascript:alert(1))"


def test_markdown_conversion_leaves_urls_snake_case_and_maths_alone():
    assert tg.to_html("open https://x.sg/a_b_c/d*e*f now") == "open https://x.sg/a_b_c/d*e*f now"
    assert tg.to_html("file_name_v2.docx and 2 * 3 = 6") == "file_name_v2.docx and 2 * 3 = 6"
    assert tg.to_html("## Plan\n---\nok") == "<b>Plan</b>\nok"
    assert tg.to_html("✅ Logged in") == "✅ Logged in"  # emoji are kept now
    assert tg.to_html("```\na < b\n```") == "<pre>a &lt; b</pre>"


def test_to_plain_is_the_readable_fallback():
    h = tg.card("📄", "A & B", tg.link("Slides", "https://x.sg/1"), tg.quote("rest <b>"))
    assert tg.to_plain(h) == "📄 A & B\nSlides (https://x.sg/1)\nrest <b>"


def test_split_never_cuts_inside_a_tag_and_respects_the_limit():
    quote = tg.quote("\n\n".join(f"paragraph {i} " * 10 for i in range(30)))  # blank lines inside the quote
    text = tg.blocks(*(f"block {i} " * 40 for i in range(20)), quote, tg.b("end"))
    parts = tg.split_html(str(text), limit=1000)
    assert len(parts) > 2 and all(utf16_len(p) <= 1000 for p in parts)
    for p in parts:
        assert p.count("<blockquote") == p.count("</blockquote>") and p.count("<b>") == p.count("</b>")


def test_an_over_long_block_falls_back_to_escaped_hard_splits():
    text = tg.b("x & y " * 500)  # one tag, longer than the limit
    parts = tg.split_html(str(text), limit=700)
    assert all(utf16_len(p) <= 700 for p in parts) and len(parts) > 1
    assert all("<b>" not in p for p in parts) and "&amp;" in parts[0]  # still escaped, tags dropped


def test_render_measures_after_html():
    parts = tg.render("<" * 3000)  # 3000 characters, 12000 after escaping
    assert len(parts) == 3 and all(utf16_len(p) <= 4096 for p in parts)
    assert "".join(parts) == "&lt;" * 3000


def test_wording_helpers():
    now = datetime(2026, 11, 13, 3, 1, tzinfo=timezone.utc)
    assert tg.until(now + timedelta(hours=2, minutes=59), now) == "in 2h 59m"
    assert tg.until(now + timedelta(minutes=29), now) == "in 29 min"
    assert tg.until(now + timedelta(hours=30), now, SGT) == "tomorrow"
    assert tg.until(now + timedelta(days=3, hours=1), now, SGT) == "in 3 days"
    assert tg.until(now - timedelta(minutes=1), now) == "overdue"
    assert tg.when(now, SGT) == "Fri 13 Nov, 11:01"
    assert tg.course("26S2-731CMBK_013682", {}) == "CMBK"
    assert tg.course("26S2-731IV_013686", {"26S2-731IV_013686": "INVESTMENTS(731IV_013686)"}) == "Investments"
    long_name = {"X": "CAREER MANAGEMENT AND BUSINESS KNOWLEDGE FOR PROFESSIONALS"}
    assert tg.course("X", long_name) == "X"
    assert tg.course(None, {}) == ""
    assert tg.tidy_title("CMBK Reflection - Fireside Chat 2026 (due by 13 Nov, 2pm)") == "CMBK Reflection – Fireside Chat 2026"
    assert tg.url_label("https://forms.office.com/r/abc") == "Microsoft Form"
    assert tg.url_label("https://www.8prasia.com/a") == "8prasia.com"


def test_body_summary_then_expandable_rest():
    text = "First sentence here. Second one. " + "More detail follows. " * 20
    out = tg.body(text)
    head, rest = out.split("\n<blockquote expandable>")
    assert head == "First sentence here. Second one." and rest.endswith("follows.</blockquote>")
    assert tg.body("Short note.") == "Short note."
    assert tg.body("  a   b \n\n\n c ") == "a b\n\nc"


class FakeBot:
    def __init__(self, refuse_html=0):
        self.sent, self.edits, self.refuse = [], [], refuse_html

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None, **kw):
        if parse_mode == "HTML" and self.refuse:
            self.refuse -= 1
            raise BadRequest("Can't parse entities: unsupported start tag \"x\" at byte offset 3")
        self.sent.append((text, parse_mode, reply_markup))
        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_text(self, text, chat_id=None, message_id=None, parse_mode=None, **kw):
        if parse_mode == "HTML" and self.refuse:
            self.refuse -= 1
            raise BadRequest("Can't parse entities")
        self.edits.append((text, parse_mode))


async def test_notifier_sends_html_mode_and_converts_plain_strings():
    bot = FakeBot()
    n = Notifier(bot, 42)
    await n.send(tg.card("📄", "Investments · New material", "Slides"))
    await n.send("**Done** <now> & then")
    assert bot.sent[0][:2] == ("📄 <b>Investments · New material</b>\nSlides", "HTML")
    assert bot.sent[1][:2] == ("<b>Done</b> &lt;now&gt; &amp; then", "HTML")


async def test_refused_html_is_resent_as_plain_text_with_its_buttons():
    bot = FakeBot(refuse_html=1)
    await Notifier(bot, 42).send(tg.card("🔴", "Due in 2h · CMBK", tg.link("Reflection", "https://x.sg/1")),
                                 buttons=[[("Do it", "doit:1")]])
    (text, mode, markup), = bot.sent
    assert mode is None and text == "🔴 Due in 2h · CMBK\nReflection (https://x.sg/1)"
    assert markup.inline_keyboard[0][0].callback_data == "doit:1"


async def test_other_telegram_errors_are_not_swallowed():
    class Gone(FakeBot):
        async def send_message(self, *a, **kw):
            raise BadRequest("Chat not found")

    with pytest.raises(BadRequest):
        await Notifier(Gone(), 42).send("hi")


async def test_edit_uses_html_and_falls_back_too():
    bot = FakeBot(refuse_html=1)
    await Notifier(bot, 42).edit(7, "Working… **step 2**")
    assert bot.edits == [("Working… step 2", None)]
    bot = FakeBot()
    await Notifier(bot, 42).edit(7, "Working… **step 2**")
    assert bot.edits == [("Working… <b>step 2</b>", "HTML")]


async def test_command_replies_use_the_same_path():
    got = []

    async def reply_text(text, **kw):
        got.append((text, kw.get("parse_mode")))

    await reply_html(SimpleNamespace(reply_text=reply_text), "Usage: /mute <course code>")
    assert got == [("Usage: /mute &lt;course code&gt;", "HTML")]
