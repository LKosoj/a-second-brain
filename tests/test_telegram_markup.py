"""Tests for attachment-image markdown helpers in telegram_markup.py."""

from d_brain.services.telegram_markup import (
    extract_attachment_image_refs,
    replace_markdown_image_links,
)


def test_extract_attachment_image_refs_finds_image_syntax_only() -> None:
    markdown = (
        "See ![chart](attachments/charts/2026-09-25-x.png) "
        "and [a link](attachments/charts/2026-09-25-x.png)."
    )

    assert extract_attachment_image_refs(markdown) == [
        "attachments/charts/2026-09-25-x.png"
    ]


def test_extract_attachment_image_refs_is_case_insensitive_on_extension() -> None:
    markdown = "![chart](attachments/charts/x.PNG)"

    assert extract_attachment_image_refs(markdown) == ["attachments/charts/x.PNG"]


def test_extract_attachment_image_refs_ignores_non_attachment_paths() -> None:
    markdown = "![a](thoughts/photo.png)"

    assert extract_attachment_image_refs(markdown) == []


def test_extract_attachment_image_refs_ignores_https_links() -> None:
    markdown = "![a](https://example.com/attachments/photo.png)"

    assert extract_attachment_image_refs(markdown) == []


def test_extract_attachment_image_refs_ignores_data_uri() -> None:
    markdown = "![a](data:image/png;base64,AAA)"

    assert extract_attachment_image_refs(markdown) == []


def test_extract_attachment_image_refs_ignores_traversal_and_absolute() -> None:
    markdown = (
        "![a](attachments/../secret.png) "
        "![b](/etc/passwd.png) "
        "![c](../attachments/photo.png) "
        "![d](attachments//x.png)"
    )

    assert extract_attachment_image_refs(markdown) == []


def test_extract_attachment_image_refs_ignores_disallowed_extension() -> None:
    markdown = "![a](attachments/evil.svg)"

    assert extract_attachment_image_refs(markdown) == []


def test_extract_attachment_image_refs_strips_title_variants() -> None:
    markdown = (
        '![a](attachments/x.png "Title") '
        "![b](attachments/y.png 'Title') "
        "![c](attachments/z.png (Title))"
    )

    assert extract_attachment_image_refs(markdown) == [
        "attachments/x.png",
        "attachments/y.png",
        "attachments/z.png",
    ]


def test_extract_attachment_image_refs_strips_angle_brackets() -> None:
    markdown = (
        "![a](<attachments/x.png>) "
        '![b](<attachments/y.png> "Title")'
    )

    assert extract_attachment_image_refs(markdown) == [
        "attachments/x.png",
        "attachments/y.png",
    ]


def test_extract_attachment_image_refs_titles_do_not_bypass_validation() -> None:
    markdown = (
        '![a](attachments/../secret.png "Title") '
        '![b](/etc/passwd.png "Title") '
        '![c](attachments/evil.svg "Title") '
        '![d](https://example.com/x.png "Title")'
    )

    assert extract_attachment_image_refs(markdown) == []


def test_replace_markdown_image_links_substitutes_resolved_value() -> None:
    markdown = "before ![chart](attachments/x.png) after"

    result = replace_markdown_image_links(markdown, lambda href: f"data:{href}")

    assert result == "before ![chart](data:attachments/x.png) after"


def test_replace_markdown_image_links_falls_back_to_alt_text_when_unresolved() -> None:
    markdown = "before ![chart](attachments/x.png) after"

    result = replace_markdown_image_links(markdown, lambda href: None)

    assert result == "before chart after"


def test_replace_markdown_image_links_resolves_title_and_angle_bracket_hrefs() -> None:
    markdown = (
        'a ![x](attachments/x.png "Title") '
        "b ![y](<attachments/y.png>)"
    )

    result = replace_markdown_image_links(markdown, lambda path: f"data:{path}")

    assert result == (
        "a ![x](data:attachments/x.png) b ![y](data:attachments/y.png)"
    )


def test_replace_markdown_image_links_leaves_non_attachment_content_unchanged() -> None:
    markdown = (
        "[link](https://example.com) and ![b](thoughts/photo.png) "
        "and ![c](attachments/x.png)"
    )

    result = replace_markdown_image_links(markdown, lambda href: None)

    assert result == (
        "[link](https://example.com) and ![b](thoughts/photo.png) and c"
    )
