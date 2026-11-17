from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.models import Item
from ops.diff import Change
from ops.format import alert_text, fmt_dt, html_to_text

SGT = ZoneInfo("Asia/Singapore")


def test_fmt_dt_converts_to_sgt():
    assert fmt_dt(datetime(2026, 10, 30, 15, 59, tzinfo=timezone.utc), SGT) == "Fri 30 Oct, 23:59"


def test_html_to_text():
    assert html_to_text("<p>Hello&nbsp;<b>class</b></p><script>x</script>") == "Hello class"


def test_html_to_text_block_spacing():
    assert html_to_text("<p>one</p><p>two</p>") == "one two"


def test_html_to_text_truncates():
    out = html_to_text("a" * 1000)
    assert len(out) <= 301
    assert out.endswith("…")


def test_alert_text_assignment():
    item = Item("brightspace", "bs:a:1", "INV2101", "assignment", "Tutorial 6",
                {"due_at": "2026-10-30T15:59:00Z"})
    now = datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)
    text = alert_text(Change("new", item), "red", SGT, now=now)
    assert text == "📌 <b>INV2101 · New assignment</b>\n<b>Tutorial 6</b>\n🗓️ Due Fri 30 Oct, 23:59 · in 4 days"


def test_alert_text_no_due_date():
    item = Item("brightspace", "bs:a:1", "INV2101", "assignment", "Reflection", {"due_at": None, "end_at": None})
    text = alert_text(Change("new", item), "red", SGT)
    assert "No due date set" in text


def test_alert_text_due_change_shows_old_and_new():
    item = Item("brightspace", "bs:a:1", "INV2101", "assignment", "T6", {"due_at": "2026-11-02T15:59:00Z"})
    c = Change("changed", item, {"due_at": ("2026-10-30T15:59:00Z", "2026-11-02T15:59:00Z")})
    text = alert_text(c, "red", SGT)
    assert text.startswith("🗓️ <b>INV2101 · Due date changed</b>")
    assert "Was Fri 30 Oct, 23:59 → now Mon 02 Nov, 23:59" in text


def test_content_alert_names_the_course_and_where_it_sits():
    item = Item("brightspace", "bs:content:7", "26S2-731IV_013686", "content", "Topic 1 homework",
                {"module": "Week 1 \u203a Homework", "type": "File", "ou": 5, "topic_id": 7})
    names = {"26S2-731IV_013686": "INVESTMENTS(731IV_013686)"}
    text = alert_text(Change("new", item), "yellow", SGT, course_names=names, base="https://lms.example")
    assert text == ("📄 <b>Investments · New material</b>\n"
                    '<a href="https://lms.example/d2l/le/content/5/viewContent/7/View">Topic 1 homework</a>\n'
                    "<i>Week 1 \u203a Homework · File</i>")


def test_alert_without_known_course_name_uses_the_tidy_code_never_the_raw_one():
    item = Item("brightspace", "bs:content:7", "26S2-731CMBK_013682", "content", "Slides", {})
    assert alert_text(Change("new", item), "yellow", SGT).startswith("📄 <b>CMBK · New material</b>\nSlides")


def test_long_post_shows_a_summary_and_keeps_the_rest_in_an_expandable_quote():
    body = "Hi class. " + "Margin is borrowed money used to buy more shares. " * 20 + "THE END"
    item = Item("teams", "t:1", None, "teams_post", "Tutor in Class", {"body_text": body})
    text = alert_text(Change("new", item), "yellow", SGT)
    head, rest = text.split("<blockquote expandable>")
    assert head.endswith("Hi class. Margin is borrowed money used to buy more shares.\n")
    assert rest.endswith("THE END</blockquote>") and len(body) > 900


def test_very_long_post_is_clipped_on_a_word_boundary():
    item = Item("teams", "t:1", None, "teams_post", "Tutor", {"body_text": "word " * 1000})
    text = alert_text(Change("new", item), "yellow", SGT)
    assert text.endswith("word\u2026</blockquote>") and len(text) < 1700


def test_raw_urls_become_short_labelled_links():
    body = ("Slides are up: https://nplms.polite.edu.sg/d2l/le/lessons/1044225/units/15416769 and the reading is at "
            "https://www.8prasia.com/article?id=3&x=1.")
    item = Item("teams", "t:1", None, "teams_post", "Tutor in Class", {"body_text": body})
    text = alert_text(Change("new", item), "yellow", SGT)
    assert '<a href="https://nplms.polite.edu.sg/d2l/le/lessons/1044225/units/15416769">POLITEMall link</a>' in text
    assert '<a href="https://www.8prasia.com/article?id=3&amp;x=1">8prasia.com</a>.' in text
    assert "https://nplms" not in text.replace('href="https://nplms', "")


def test_markup_inside_a_post_renders_literally():
    body = 'Read this <b>now</b></blockquote><a href="https://evil.example">x</a> & "quote" ' + "pad " * 80
    item = Item("teams", "t:1", None, "teams_post", "<i>Tutor</i> in Class", {"body_text": body})
    text = alert_text(Change("new", item), "yellow", SGT)
    assert "&lt;b&gt;now&lt;/b&gt;&lt;/blockquote&gt;&lt;a href=" in text and "&gt;x&lt;/a&gt;" in text
    assert '<a href="https://evil.example">x</a>' not in text  # the URL itself is only a labelled link
    assert "&lt;i&gt;Tutor&lt;/i&gt;" in text and "&amp;" in text
    assert text.count("<blockquote") == 1 and text.count("</blockquote>") == 1


def test_class_chat_action_names_the_group_and_due_date():
    item = Item("group", "group:action:1", None, "group_action", "Submit the peer review form",
                {"body_text": "Deadline: Fri 23:59", "group": "WISP", "due": "Fri 23:59"})
    assert alert_text(Change("new", item), "red", SGT) == (
        "💬 <b>WISP · action needed</b>\nSubmit the peer review form\n🗓️ Due Fri 23:59")
    old = Item("group", "group:action:2", None, "group_action", "Pay for the trip", {"body_text": "", "group": "WISP"})
    assert alert_text(Change("new", old), "red", SGT) == "💬 <b>WISP · action needed</b>\nPay for the trip"


def test_email_card():
    item = Item("outlook", "outlook:1", None, "email", "Quiz <2> moved",
                {"sender": "Mr Tan", "preview": "Now Block L.", "category": "important",
                 "link": "https://outlook.office.com/mail/1"})
    assert alert_text(Change("new", item), "red", SGT) == (
        "✉️ <b>Important email · Mr Tan</b>\n<b>Quiz &lt;2&gt; moved</b>\n\nNow Block L.\n\n"
        '<a href="https://outlook.office.com/mail/1">Open in Outlook</a>')
