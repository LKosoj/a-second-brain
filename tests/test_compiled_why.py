"""Tests for the owner-facing "/why" provenance answer (задача L, ТЗ 7.3/7.4).

``build_why``/``build_why_for_path``/``resolve_why_target`` are pure and
read-only (see module docstring), so every test here only assembles a
temporary ``compiled/**`` tree -- no vault write path is exercised, and no
model is called.
"""

from __future__ import annotations

from pathlib import Path

from d_brain.services.compiled_briefings import CompiledBriefingService
from d_brain.services.compiled_why import (
    AMBIGUITY_RATIO,
    WhyOutcome,
    build_why,
    build_why_for_path,
)


def _page_text(
    *,
    domain: str,
    title: str,
    description: str = "",
    tier: str = "active",
    relevance: float = 0.80,
    sources_trust: str | None = None,
    last_verified: str = "",
    human_reviewed: str = "",
    enrichment_count: int = 0,
    sources_rows: list[tuple[str, str, str]] | None = None,
    claim_history_rows: list[tuple[str, str, str, str]] | None = None,
    conflicts_rows: list[tuple[str, str, str, str, str]] | None = None,
) -> str:
    """Render one ``compiled/**`` page in the shape ``CompiledBriefingService``
    itself writes and parses, using its own table renderers, so the real
    parsers this module reuses exercise real regexes instead of a
    hand-rolled shortcut (mirrors ``tests/test_compiled_briefs.py``).
    """
    frontmatter = [
        "---",
        "type: compiled-briefing",
        f"domain: {domain}",
        f'description: "{description or title}"',
        "status: active",
        "created: 2026-07-01",
        "updated: 2026-07-01",
        "freshness_state: fresh",
        "confidence: high",
    ]
    if sources_trust is not None:
        frontmatter.append(f"sources_trust: {sources_trust}")
    frontmatter += [
        f"last_verified: {last_verified}",
        f"enrichment_count: {enrichment_count}",
        f"human_reviewed: {human_reviewed}",
        "last_accessed: 2026-07-01",
        f"relevance: {relevance:.2f}",
        f"tier: {tier}",
        "---",
        "",
    ]
    body = [f"# {title}", "", "## Sources That Shaped This Page", ""]
    body += CompiledBriefingService._render_sources_shaped_table(sources_rows or [])
    if claim_history_rows:
        body += ["", "## Claim History", ""]
        body += [
            "| Date | Source | Claim | Superseded By |",
            "| --- | --- | --- | --- |",
        ]
        body += [
            f"| {row_date} | [[{old_source}]] | {claim} | [[{new_source}]] |"
            for row_date, old_source, claim, new_source in claim_history_rows
        ]
    if conflicts_rows:
        body += ["", "## Open Conflicts", ""]
        body += CompiledBriefingService._render_open_conflicts_table(conflicts_rows)
    return "\n".join(frontmatter) + "\n".join(body) + "\n"


def _write_page(vault: Path, rel_path: str, **kwargs: object) -> None:
    path = vault / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_page_text(**kwargs), encoding="utf-8")  # type: ignore[arg-type]


def _why_section(markdown: str, heading: str) -> list[str]:
    """The lines under one ``**Heading**`` of a rendered "/why" answer --
    same purpose as ``tests/test_compiled_briefs.py``'s ``_brief_section``:
    a bare substring assert cannot tell which section a fact landed in."""
    lines = markdown.split("\n")
    start = lines.index(f"**{heading}**")
    section: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("**") and line.endswith("**"):
            break
        if line:
            section.append(line)
    return section


# --- rendering: what is shown, and what is deliberately never shown ---------


def test_build_why_renders_sources_trust_history_conflicts_and_verification(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/decisions/acme-renewal.md",
        domain="decisions",
        title="Продление контракта Acme",
        description="Продлить контракт с Acme на год",
        sources_trust="own",
        last_verified="2026-07-01",
        human_reviewed="2026-06-01",
        enrichment_count=3,
        sources_rows=[
            ("2026-06-01", "daily/2026-06-01.md", "Первое упоминание"),
            ("2026-06-15", "daily/2026-06-15.md", "Подтверждение цены"),
        ],
        claim_history_rows=[
            (
                "2026-06-20",
                "daily/2026-06-10.md",
                "Цена 100у.е.",
                "daily/2026-06-20.md",
            )
        ],
        conflicts_rows=[
            (
                "2026-06-25",
                "Срок год",
                "daily/a.md",
                "Срок два года",
                "daily/b.md",
            )
        ],
    )

    outcome = build_why(vault, "acme-renewal")

    assert outcome.status == "resolved"
    assert outcome.result is not None
    markdown = outcome.result.markdown
    assert "Уровень доверия источников страницы: own." in markdown
    # Each fact under its own heading: a plain "in markdown" assert would
    # still pass if the history rows were rendered under "Источники".
    assert _why_section(markdown, "Источники (по датам)") == [
        "- 2026-06-01 · [[daily/2026-06-01.md]] — Первое упоминание",
        "- 2026-06-15 · [[daily/2026-06-15.md]] — Подтверждение цены",
    ]
    assert _why_section(markdown, "История утверждений (замены)") == [
        "- 2026-06-20: было «Цена 100у.е.» (источник [[daily/2026-06-10.md]]) "
        "→ заменено [[daily/2026-06-20.md]]"
    ]
    assert _why_section(markdown, "Открытые конфликты") == [
        "- 2026-06-25: «Срок год» (источник [[daily/a.md]]) vs "
        "«Срок два года» (источник [[daily/b.md]])"
    ]
    verification = _why_section(markdown, "Проверка страницы")
    assert verification[0].startswith("Последняя полная проверка: 2026-07-01")
    assert "Подтверждение владельцем: 2026-06-01." in verification
    assert "Число проходов обогащения: 3." in verification


def test_build_why_never_fabricates_per_claim_verification_status(
    tmp_path, write_vault_manifest
):
    """ТЗ requirement: per-claim verification status is never stored, so the
    answer must say so explicitly rather than inventing a per-claim list."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        domain="topics",
        title="Aurora",
        last_verified="2026-07-01",
        sources_rows=[("2026-06-01", "daily/2026-06-01.md", "x")],
    )

    outcome = build_why(vault, "aurora")

    assert outcome.result is not None
    markdown = outcome.result.markdown
    assert (
        "какие именно утверждения проверялись — не записывается" in markdown.lower()
    )


def test_build_why_missing_last_verified_and_human_reviewed_says_not_recorded(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        domain="topics",
        title="Aurora",
        sources_rows=[("2026-06-01", "daily/2026-06-01.md", "x")],
    )

    outcome = build_why(vault, "aurora")

    assert outcome.result is not None
    markdown = outcome.result.markdown
    assert "Последняя полная проверка: не зафиксирована" in markdown
    assert "Подтверждение владельцем: не подтверждалось" in markdown


def test_build_why_low_trust_shows_warning_callout(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        domain="topics",
        title="Aurora",
        sources_trust="forwarded",
        sources_rows=[("2026-06-01", "daily/2026-06-01.md", "x")],
    )

    outcome = build_why(vault, "aurora")

    assert outcome.result is not None
    assert "Внимание" in outcome.result.markdown


def test_build_why_no_open_conflicts_says_so(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        domain="topics",
        title="Aurora",
        sources_rows=[("2026-06-01", "daily/2026-06-01.md", "x")],
    )

    outcome = build_why(vault, "aurora")

    assert outcome.result is not None
    assert "открытых конфликтов нет" in outcome.result.markdown


# --- target resolution: domain=None searches all six domains ----------------


def test_build_why_exact_match_searches_all_six_domains_not_just_one(
    tmp_path, write_vault_manifest
):
    """Unlike a brief (fixed domain per type), "/why" has no target domain
    -- an exact slug match in ``people`` (a domain briefs never touch) must
    still resolve."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/people/ivan-petrov.md",
        domain="people",
        title="Иван Петров",
        sources_rows=[("2026-06-01", "daily/2026-06-01.md", "x")],
    )

    outcome = build_why(vault, "ivan-petrov")

    assert outcome.status == "resolved"
    assert outcome.result is not None
    assert outcome.result.domain == "people"
    assert outcome.result.rel_path == "compiled/people/ivan-petrov.md"


def test_build_why_not_found_returns_not_found_status_not_exception(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "compiled").mkdir(parents=True)

    outcome = build_why(vault, "не существующая страница вообще")

    assert outcome == WhyOutcome(status="not_found")


def test_build_why_empty_query_returns_not_found(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        domain="topics",
        title="Aurora",
        sources_rows=[("2026-06-01", "daily/2026-06-01.md", "x")],
    )

    outcome = build_why(vault, "")

    assert outcome.status == "not_found"


# --- target resolution: ambiguity heuristic ----------------------------------


def test_resolve_why_target_ambiguous_when_top_two_share_query_terms_closely(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/decisions/vendor-renewal-a.md",
        domain="decisions",
        title="Продление контракта с поставщиком А",
        description="Продление контракта с поставщиком А на год",
        sources_rows=[("2026-06-01", "daily/2026-06-01.md", "x")],
    )
    _write_page(
        vault,
        "compiled/decisions/vendor-renewal-b.md",
        domain="decisions",
        title="Продление контракта с поставщиком Б",
        description="Продление контракта с поставщиком Б на год",
        sources_rows=[("2026-06-01", "daily/2026-06-01.md", "x")],
    )

    outcome = build_why(vault, "продление контракта с поставщиком")

    assert outcome.status == "ambiguous"
    assert outcome.result is None
    assert len(outcome.choices) == 2
    rel_paths = {choice.rel_path for choice in outcome.choices}
    assert rel_paths == {
        "compiled/decisions/vendor-renewal-a.md",
        "compiled/decisions/vendor-renewal-b.md",
    }


def test_same_slug_in_two_domains_is_asked_about_not_guessed(
    tmp_path, write_vault_manifest
):
    """A slug is unique inside one domain only. ``/why`` searches every
    domain at once, so an exact slug hit in two of them is an ambiguity --
    answering about whichever page sorts first would explain the wrong page
    while looking perfectly confident."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/decisions/roadmap.md",
        domain="decisions",
        title="Решение по дорожной карте",
        sources_rows=[("2026-06-01", "daily/2026-06-01.md", "x")],
    )
    _write_page(
        vault,
        "compiled/meetings/roadmap.md",
        domain="meetings",
        title="Встреча по дорожной карте",
        sources_rows=[("2026-06-02", "daily/2026-06-02.md", "y")],
    )

    outcome = build_why(vault, "roadmap")

    assert outcome.status == "ambiguous"
    assert outcome.result is None
    assert {choice.rel_path for choice in outcome.choices} == {
        "compiled/decisions/roadmap.md",
        "compiled/meetings/roadmap.md",
    }
    assert {choice.domain for choice in outcome.choices} == {
        "decisions",
        "meetings",
    }


def test_same_slug_in_three_domains_offers_all_three(tmp_path, write_vault_manifest):
    """There are six ``COMPILED_BRIEFING_DOMAINS``, so a slug can land in
    three or more of them. Offering only the first two left the rest
    unreachable through ``/why`` entirely -- and the owner was told the
    answer lay between two pages while a third, equally exact match was
    silently dropped. Every exact match is equally exact; there is no
    ranking here that could justify picking which two survive."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    for domain, title in (
        ("decisions", "Решение по дорожной карте"),
        ("meetings", "Встреча по дорожной карте"),
        ("topics", "Тема дорожной карты"),
    ):
        _write_page(
            vault,
            f"compiled/{domain}/roadmap.md",
            domain=domain,
            title=title,
            sources_rows=[("2026-06-01", "daily/2026-06-01.md", "x")],
        )

    outcome = build_why(vault, "roadmap")

    assert outcome.status == "ambiguous"
    assert outcome.result is None
    assert {choice.rel_path for choice in outcome.choices} == {
        "compiled/decisions/roadmap.md",
        "compiled/meetings/roadmap.md",
        "compiled/topics/roadmap.md",
    }


def test_two_pages_matched_only_through_their_bodies_are_put_to_the_owner(
    tmp_path, write_vault_manifest
):
    """``_rank_candidates`` scores body text too, so a query can rank two
    pages while matching neither title nor description. The closeness proxy
    used to read only title+description, scored both at zero, fell through
    the ``top_score > 0`` guard, and /why answered confidently about
    whichever page happened to sort first -- exactly the guessing ТЗ 7.3
    forbids."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/alpha.md",
        domain="topics",
        title="Альфа",
        description="Записи по направлению Альфа",
        sources_rows=[
            (
                "2026-06-01",
                "daily/2026-06-01.md",
                "Обсуждали перенос склада в Роттердам подробно и не раз",
            )
        ],
    )
    _write_page(
        vault,
        "compiled/projects/beta.md",
        domain="projects",
        title="Бета",
        description="Работы по направлению Бета",
        sources_rows=[
            (
                "2026-06-02",
                "daily/2026-06-02.md",
                "Обсуждали перенос склада в Роттердам на прошлой неделе",
            )
        ],
    )

    outcome = build_why(vault, "перенос склада Роттердам")

    assert outcome.status == "ambiguous"
    assert {choice.rel_path for choice in outcome.choices} == {
        "compiled/topics/alpha.md",
        "compiled/projects/beta.md",
    }


def test_a_passing_body_mention_does_not_turn_a_clear_answer_into_a_question(
    tmp_path, write_vault_manifest
):
    """The other side of the body-token fix: counting body tokens as heavily
    as title tokens made a page that merely mentions the query in passing
    tie with the page the query actually names, and /why started asking the
    owner to choose where it used to answer. The proxy keeps the ranker's
    own weights so a title hit still outweighs a body one."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/logistika-sklada.md",
        domain="topics",
        title="Логистика склада",
        description="Как устроена складская логистика",
        sources_rows=[("2026-06-01", "daily/2026-06-01.md", "Работаем по схеме")],
    )
    _write_page(
        vault,
        "compiled/topics/raznoe.md",
        domain="topics",
        title="Разное",
        description="Мелкие заметки",
        sources_rows=[
            (
                "2026-06-02",
                "daily/2026-06-02.md",
                "Мимоходом всплыл перенос склада в Роттердам, к делу не относится",
            )
        ],
    )

    outcome = build_why(vault, "перенос склада Роттердам")

    assert outcome.status == "resolved"
    assert outcome.result is not None
    assert outcome.result.rel_path == "compiled/topics/logistika-sklada.md"


def test_a_page_whose_title_is_the_query_is_not_put_up_for_a_vote(
    tmp_path, write_vault_manifest
):
    """The ranker's two strongest text terms are the +8 for a title
    contained whole in the query and the +5 for the same of a slug. Left
    out of the proxy, a page whose title *is* the query scored under one
    that only name-drops it in its description, and /why asked the owner to
    choose between a pair the ranker had ordered decisively."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/projects/avrora.md",
        domain="projects",
        title="Аврора",
        description="проект",
        sources_rows=[("2026-06-01", "daily/2026-06-01.md", "Ход работ")],
    )
    _write_page(
        vault,
        "compiled/projects/status-proektov.md",
        domain="projects",
        title="Статус проектов",
        description="статус проекта аврора и других",
        sources_rows=[("2026-06-02", "daily/2026-06-02.md", "Сводка")],
    )

    outcome = build_why(vault, "аврора статус проекта")

    assert outcome.status == "resolved"
    assert outcome.result is not None
    assert outcome.result.rel_path == "compiled/projects/avrora.md"


def test_a_title_asked_about_in_an_oblique_case_still_earns_the_title_bonus(
    tmp_path, write_vault_manifest
):
    """Pins the loose containment test the title/slug bonuses rely on. Titles
    are written in the nominative and questions are not, and nothing here
    stems words, so every attempt to tighten that test -- to a word boundary,
    then to a boundary plus a short case ending -- dropped "Краснодар" from
    queries like this one: the page then scored zero, was cut before ranking,
    and /why answered confidently off a page that only shared the query's
    other words."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/krasnodar.md",
        domain="topics",
        title="Краснодар",
        description="южный филиал",
        sources_rows=[("2026-06-01", "daily/2026-06-01.md", "Команда и офис")],
    )
    _write_page(
        vault,
        "compiled/topics/bilety.md",
        domain="topics",
        title="Билеты и перелёты",
        description="цены на билеты и маршруты",
        sources_rows=[("2026-06-02", "daily/2026-06-02.md", "Общие заметки")],
    )

    # A case ending and a relative adjective: the two commonest ways a
    # question refers to a page without repeating its title verbatim. Both
    # are checked because the second needs a longer tail than the first, and
    # a rule wide enough for one was still narrow enough to lose the other.
    for query in ("билеты до краснодара", "что там по краснодарской команде"):
        outcome = build_why(vault, query)

        assert outcome.status == "resolved", query
        assert outcome.result is not None
        assert outcome.result.rel_path == "compiled/topics/krasnodar.md", query


def test_a_slug_unique_across_domains_still_resolves_without_asking(
    tmp_path, write_vault_manifest
):
    """The collision check must not turn every exact slug hit into a
    question: one page owning the slug still answers straight away."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/decisions/roadmap.md",
        domain="decisions",
        title="Решение по дорожной карте",
        sources_rows=[("2026-06-01", "daily/2026-06-01.md", "x")],
    )
    _write_page(
        vault,
        "compiled/meetings/outage.md",
        domain="meetings",
        title="Инцидент с отказом сервиса",
        sources_rows=[("2026-06-02", "daily/2026-06-02.md", "y")],
    )

    outcome = build_why(vault, "roadmap")

    assert outcome.status == "resolved"
    assert outcome.choices == ()
    assert outcome.result is not None
    assert outcome.result.rel_path == "compiled/decisions/roadmap.md"


def test_an_exact_path_query_beats_a_slug_collision(tmp_path, write_vault_manifest):
    """Two pages share the slug, but the owner named a full path -- a path
    is unique, so this is not ambiguous and must not be put back to them."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/decisions/roadmap.md",
        domain="decisions",
        title="Решение по дорожной карте",
        sources_rows=[("2026-06-01", "daily/2026-06-01.md", "x")],
    )
    _write_page(
        vault,
        "compiled/meetings/roadmap.md",
        domain="meetings",
        title="Встреча по дорожной карте",
        sources_rows=[("2026-06-02", "daily/2026-06-02.md", "y")],
    )

    outcome = build_why(vault, "vault/compiled/meetings/roadmap.md")

    assert outcome.status == "resolved"
    assert outcome.result is not None
    assert outcome.result.rel_path == "compiled/meetings/roadmap.md"


def test_resolve_why_target_resolved_when_runner_up_is_a_weak_match(
    tmp_path, write_vault_manifest
):
    """Same shape as the ambiguous test, but the runner-up only shares one
    query term instead of nearly all of them -- below ``AMBIGUITY_RATIO``,
    so this must resolve to the clear winner instead of asking."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/decisions/vendor-renewal.md",
        domain="decisions",
        title="Продление контракта с поставщиком",
        description="Продление контракта с поставщиком на год",
        sources_rows=[("2026-06-01", "daily/2026-06-01.md", "x")],
    )
    _write_page(
        vault,
        "compiled/topics/unrelated-delivery.md",
        domain="topics",
        title="Заметка про доставку кофе по вторникам",
        description="Кофе для другого поставщика, план доставки на месяц",
        sources_rows=[("2026-06-01", "daily/2026-06-01.md", "x")],
    )

    outcome = build_why(vault, "продление контракта с поставщиком")

    assert outcome.status == "resolved"
    assert outcome.result is not None
    assert outcome.result.rel_path == "compiled/decisions/vendor-renewal.md"


def test_ambiguity_ratio_is_fixed_and_documented():
    """Guards the documented heuristic constant against an accidental,
    silent change (задача L requires it be fixed in a code comment)."""
    assert AMBIGUITY_RATIO == 0.7


# --- build_why_for_path: direct render, no resolution ------------------------


def test_build_why_for_path_renders_directly(tmp_path, write_vault_manifest):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        domain="topics",
        title="Aurora",
        sources_rows=[("2026-06-01", "daily/2026-06-01.md", "Что-то важное")],
    )

    result = build_why_for_path(vault, "compiled/topics/aurora.md")

    assert result is not None
    assert result.title == "Aurora"
    assert result.domain == "topics"
    assert "Что-то важное" in result.markdown


def test_build_why_for_path_returns_none_when_page_missing(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    (vault / "compiled").mkdir(parents=True)

    assert build_why_for_path(vault, "compiled/topics/missing.md") is None


def test_build_why_for_path_used_for_ambiguous_choice_matches_build_why_result(
    tmp_path, write_vault_manifest
):
    """The bot's disambiguation callback calls ``build_why_for_path`` with
    the exact ``rel_path`` from a ``WhyChoice`` -- must render identically
    to what a direct, unambiguous ``build_why`` call would have produced."""
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _write_page(
        vault,
        "compiled/topics/aurora.md",
        domain="topics",
        title="Aurora",
        sources_rows=[("2026-06-01", "daily/2026-06-01.md", "x")],
    )

    direct = build_why(vault, "compiled/topics/aurora.md")
    via_path = build_why_for_path(vault, "compiled/topics/aurora.md")

    assert direct.result is not None
    assert via_path is not None
    assert direct.result.markdown == via_path.markdown
