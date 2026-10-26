import subprocess
import sys

from d_brain.services import compiled_page
from d_brain.services.compiled_page import (
    body_without_frontmatter,
    frontmatter_fields,
)
from d_brain.services.frontmatter import (
    parse_frontmatter_bytes,
    split_frontmatter_text,
)


def test_frontmatter_fields_reads_plain_lines() -> None:
    text = "---\ntype: project\ntier: warm\n---\n# Title\n"

    assert frontmatter_fields(text) == {"type": "project", "tier": "warm"}


def test_frontmatter_fields_decodes_json_quoted_value() -> None:
    text = '---\ntitle: "Иван \\"Ваня\\" Петров"\n---\nbody\n'

    assert frontmatter_fields(text) == {"title": 'Иван "Ваня" Петров'}


def test_frontmatter_fields_accepts_crlf_and_bom() -> None:
    assert frontmatter_fields("---\r\ntier: warm\r\n---\r\nbody\r\n") == {
        "tier": "warm"
    }
    assert frontmatter_fields("﻿---\ntier: warm\n---\nbody\n") == {
        "tier": "warm"
    }


def test_frontmatter_fields_without_header_or_closing_is_empty() -> None:
    assert frontmatter_fields("# Title\n") == {}
    assert frontmatter_fields("") == {}
    assert frontmatter_fields("---\ntier: warm\n# no closing line\n") == {}


def test_frontmatter_fields_folds_block_scalar_description() -> None:
    text = (
        "---\n"
        "description: >-\n"
        "  First line\n"
        "  second line\n"
        "tier: warm\n"
        "---\n"
        "body\n"
    )

    fields = frontmatter_fields(text)

    assert fields["description"] == "First line second line"
    assert fields["tier"] == "warm"


def test_frontmatter_fields_keeps_newline_of_literal_block_scalar() -> None:
    text = "---\ndescription: |\n  First\n  second\ntier: warm\n---\nbody\n"

    fields = frontmatter_fields(text)

    assert fields["description"] == "First\nsecond"
    assert fields["tier"] == "warm"


def test_frontmatter_fields_ignores_indented_lines_inside_block() -> None:
    text = (
        "---\n"
        "description: >-\n"
        "  status: not a key\n"
        "  tier: also not a key\n"
        "status: active\n"
        "---\n"
        "body\n"
    )

    fields = frontmatter_fields(text)

    assert fields["status"] == "active"
    assert "tier" not in fields
    assert fields["description"] == "status: not a key tier: also not a key"


def test_frontmatter_fields_keeps_scalars_as_written() -> None:
    text = "---\ntags: [a, b]\nrelevance: 0.80\nreviewed: true\n---\nbody\n"

    assert frontmatter_fields(text) == {
        "tags": "[a, b]",
        "relevance": "0.80",
        "reviewed": "true",
    }


def test_frontmatter_fields_survives_broken_header() -> None:
    duplicate = "---\ntier: warm\ntier: cold\n---\nbody\n"
    broken = "---\ntier: [warm\nstatus: active\n---\nbody\n"

    assert frontmatter_fields(duplicate) == {"tier": "cold"}
    assert frontmatter_fields(broken) == {"tier": "[warm", "status": "active"}


def test_frontmatter_fields_agrees_with_yaml_parser_on_plain_scalars() -> None:
    samples = [
        "---\ntype: project\nstatus: active\ndomain: topics\n---\nbody\n",
        '---\ntitle: "Quoted: value"\nstatus: done\n---\nbody\n',
        "---\nenrichment_count: 3\ntier: core\n---\nbody\n",
    ]
    for text in samples:
        parsed = parse_frontmatter_bytes(text.encode("utf-8")).fields

        assert frontmatter_fields(text) == {k: str(v) for k, v in parsed.items()}


def test_body_without_frontmatter_keeps_crlf_in_body() -> None:
    text = "---\r\ntier: warm\r\n---\r\nline one\r\nline two\r\n"

    assert body_without_frontmatter(text) == "line one\r\nline two\r\n"
    assert body_without_frontmatter("no header\r\n") == "no header\r\n"


def test_compiled_page_does_not_import_compiled_briefings_at_import_time() -> None:
    code = (
        "import sys, d_brain.services.compiled_page; "
        "sys.exit(int('d_brain.services.compiled_briefings' in sys.modules))"
    )

    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


def test_section_helpers_forward_to_the_service() -> None:
    text = "# T\n\n## Claim History\n\n- one\n- two\n\n## Sources\n\n- x\n"

    assert compiled_page.has_section(text, "Claim History") is True
    assert compiled_page.section_bullets(text, "Claim History") == ["one", "two"]
    assert "- x" in compiled_page.section_text(text, "Sources")


def test_split_frontmatter_text_keeps_bom_and_blank_lines_in_header() -> None:
    text = "﻿---\na: 1\n---\n\n\nbody\n"

    header, body = split_frontmatter_text(text)

    assert header == "﻿---\na: 1\n---\n\n\n"
    assert body == "body\n"
    assert header + body == text


def test_split_frontmatter_text_without_closing_line_is_all_body() -> None:
    text = "---\na: 1\nbody\n"

    assert split_frontmatter_text(text) == ("", text)


def test_no_module_outside_compiled_briefings_reaches_into_service_privates() -> None:
    """The service's private methods are not an API for sibling modules."""
    import ast
    from pathlib import Path

    package = Path(compiled_page.__file__).resolve().parents[1]
    source = (package / "services" / "compiled_briefings.py").read_text("utf-8")
    service = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef) and node.name == "CompiledBriefingService"
    )
    private = {
        item.name
        for item in service.body
        if isinstance(item, ast.FunctionDef) and item.name.startswith("_")
    }
    offenders = []
    for path in sorted(package.rglob("*.py")):
        if path.name == "compiled_briefings.py":
            continue
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if not isinstance(node, ast.Attribute) or node.attr not in private:
                continue
            base = ast.unparse(node.value)
            # compiled_page forwards the human-zone helpers on purpose (see the
            # note above its section wrappers); ``self``/``cls`` are other classes.
            if base in {"self", "cls"} or (
                path.name == "compiled_page.py" and base == "_service()"
            ):
                continue
            if base.split(".")[-1] in {"CompiledBriefingService", "service"}:
                offenders.append(f"{path.name}:{node.lineno}: {base}.{node.attr}")

    assert offenders == []


def test_no_module_imports_a_private_name_from_another_d_brain_module() -> None:
    """``from d_brain.x import _name`` makes a private helper into a hidden
    API; the owner module must publish it without the underscore instead.
    Also covers ``compiled_x._name`` access between ``compiled_*`` modules."""
    import ast
    from pathlib import Path

    package = Path(compiled_page.__file__).resolve().parents[1]
    # Explicit exceptions: "<file relative to d_brain>:<module>:<name>" -> why.
    allowed: dict[str, str] = {}
    compiled_modules = {
        path.stem for path in (package / "services").glob("compiled_*.py")
    }
    offenders = []
    for path in sorted(package.rglob("*.py")):
        rel = path.relative_to(package).as_posix()
        tree = ast.parse(path.read_text("utf-8"))
        # Local name -> compiled_* module it is bound to, import aliases included.
        bound: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                for alias in node.names:
                    stem = alias.name.rsplit(".", 1)[-1]
                    if stem in compiled_modules and (
                        alias.asname or isinstance(node, ast.ImportFrom)
                    ):
                        bound[alias.asname or stem] = stem
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (
                node.level or (node.module or "").startswith("d_brain")
            ):
                for alias in node.names:
                    key = f"{rel}:{node.module}:{alias.name}"
                    if alias.name.startswith("_") and key not in allowed:
                        offenders.append(f"{rel}:{node.lineno}: {key}")
            elif (
                isinstance(node, ast.Attribute)
                and node.attr.startswith("_")
                and not node.attr.startswith("__")
                and isinstance(node.value, ast.Name)
                and bound.get(node.value.id, path.stem) != path.stem
            ):
                offenders.append(f"{rel}:{node.lineno}: {node.value.id}.{node.attr}")

    assert offenders == []


def test_frontmatter_fields_skips_yaml_for_flat_headers(monkeypatch) -> None:
    def boom(_raw: bytes) -> None:
        raise AssertionError("YAML must not run for a flat header")

    monkeypatch.setattr(compiled_page, "parse_frontmatter_bytes", boom)

    text = "---\ntype: project\ntags: [a, b]\n---\nbody\n"
    assert frontmatter_fields(text) == {"type": "project", "tags": "[a, b]"}


def test_frontmatter_fields_still_reads_block_scalar_with_yaml() -> None:
    text = "---\ndescription: >-\n  one\n  two: x\ntier: warm\n---\nbody\n"

    assert frontmatter_fields(text) == {"description": "one two: x", "tier": "warm"}
