---
type: note
description: Human-zone protection and provenance-table conventions for compiled/ pages -- what graph and link-repair tooling must never touch, and what /why surfaces from those tables. Mirrors src/d_brain/services/compiled_briefings.py; do not restate it as an independent spec.
last_accessed: 2026-08-05
relevance: 0.6
tier: warm
---

# Links And Provenance Policy

## The Human Zone

Every compiled page's `## Owner Notes` section wraps one block in HTML
comment markers:

```
<!-- human:start -->
...owner-written text...
<!-- human:end -->
```

Everything between those two markers is owned by the human, not by the
pipeline. It survives every recompilation, archival, and link-repair pass
**verbatim** -- byte for byte, including any links inside it.

This is why `vault-health/scripts/fix_links.py` special-cases it
(`_protect_human_zone`): a blind whole-file regex substitution used to be
able to silently rewrite text the owner typed by hand. The markers are
imported directly from `compiled_briefings.HUMAN_ZONE_START` /
`HUMAN_ZONE_END` rather than duplicated as literals, so the two stay in
sync by construction. Any future vault-wide text-rewriting tool must apply
the same protection -- treat "does this touch bytes inside
`<!-- human:start -->` / `<!-- human:end -->`?" as a standing question for
new maintenance scripts, not just this one.

## Provenance Tables

Three tables carry the page's evidence trail, each already recording its
own reason in an adjacent cell:

- **Sources That Shaped This Page** -- `| date | [[source]] | what changed |`
- **Claim History** -- `| date | [[old source]] | claim | [[new source]] |`
  (supersession record: what replaced what, and why)
- **Open Conflicts** -- `| date | existing claim | [[existing source]] | new
  claim | [[new source]] |` (both sides of an unresolved factual
  disagreement; contextual conflicts resolve automatically and never get a
  row here)

These rows are a **generated ledger**, not a set of links for a human or an
agent to curate. `graph-builder`'s "link from meaning" / mention-based
linking judgment does not apply to them -- see the "Do Not" rule added to
`graph-builder/SKILL.md`.

## What `/why` Surfaces

`compiled_why.build_why` / `build_why_for_path` render, per page: the
sources table, the trust level, the claim-history table, open conflicts
(both sides), the date of the page's last full verification
(`last_verified`), the enrichment count, and whether/when the owner last
confirmed the page (`human_reviewed`).

One thing it deliberately never shows: a claim-by-claim "this specific claim
was checked" list. Production never records *which* claims a verification
pass actually checked -- only a page-level `last_verified` date exists. So
`/why` states that date with an explicit note that per-claim verification
status is not recorded, rather than fabricating one.
