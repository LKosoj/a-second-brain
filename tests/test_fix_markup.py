"""Regression tests: emphasis regexes must not corrupt code spans.

Audit finding: `markdown_to_html` applied the bold/italic regexes after
`<code>` spans were already produced, so `*` characters inside inline code
(e.g. `` `*.md` ``) were misread as emphasis markers. This also made a lone
`*` used as a multiplication operator (`5 * 3 * 2`) turn italic, and made
`markdown_to_markdown_v2` emit unbalanced backticks/underscores that Telegram
rejects outright.
"""

from d_brain.services.telegram_markup import markdown_to_html, markdown_to_markdown_v2


def test_code_span_with_asterisks_is_not_split_into_italic() -> None:
    html = markdown_to_html("Файлы `*.md` и `*.txt`")

    assert html == "Файлы <code>*.md</code> и <code>*.txt</code>"


def test_bold_markers_inside_code_span_stay_literal() -> None:
    html = markdown_to_html("code `**bold**`")

    assert html == "code <code>**bold**</code>"


def test_lone_asterisk_operator_does_not_open_italic() -> None:
    html = markdown_to_html("5 * 3 * 2 = 30")

    assert html == "5 * 3 * 2 = 30"
    assert "<i>" not in html


def test_markdown_to_markdown_v2_balances_backticks_for_code_with_asterisks() -> None:
    v2 = markdown_to_markdown_v2("Файлы `*.md` и `*.txt`")

    assert v2.count("`") % 2 == 0
    assert v2 == "Файлы `*.md` и `*.txt`"
