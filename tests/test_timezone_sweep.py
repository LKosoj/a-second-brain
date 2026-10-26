"""Guard: the owner's calendar day comes from ``d_brain.clock`` only.

A new ``date.today()`` or a bare ``datetime.now()`` silently brings back the
system zone, so a UTC server files an entry under the wrong day. Technical
timestamps with an explicit offset (``datetime.now().astimezone().isoformat()``
and friends) are fine and not matched here.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "d_brain"

PATTERNS = {
    "date.today()": re.compile(r"\bdate\.today\("),
    "datetime.now() without a zone": re.compile(
        r"\bdatetime\.now\(\)(?!\s*\.\s*astimezone\()"
    ),
    "message.date.astimezone()": re.compile(r"message\.date\.astimezone\("),
}

# (file relative to src/d_brain, pattern name) -> why it stays on system time.
ALLOWED = {
    ("services/frontmatter.py", "datetime.now() without a zone"): (
        "run_id is a technical identifier, not the owner's day"
    ),
    ("services/processor.py", "date.today()"): (
        "age of the graph file by mtime, a duration rather than the owner's day"
    ),
}


def _code(path: Path) -> str:
    """Source without comment lines; line numbers are kept (blank lines)."""
    return "\n".join(
        "" if line.lstrip().startswith("#") else line
        for line in path.read_text("utf-8").splitlines()
    )


def test_owner_day_comes_from_clock_only() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if relative == "clock.py" or relative.startswith("resources/"):
            continue
        code = _code(path)
        for name, pattern in PATTERNS.items():
            if (relative, name) in ALLOWED:
                continue
            for match in pattern.finditer(code):
                number = code.count("\n", 0, match.start()) + 1
                offenders.append(f"{relative}:{number}: {name}")
    assert offenders == [], "use d_brain.clock instead:\n" + "\n".join(offenders)


def test_allowed_exceptions_still_exist() -> None:
    for relative, name in ALLOWED:
        text = (SRC / relative).read_text("utf-8")
        assert PATTERNS[name].search(text), f"stale exception {relative} {name}"
