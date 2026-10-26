"""Direct tests for ``json_normalizer`` (model/CLI output -> JSON objects)."""

import json

import pytest

from d_brain.services.json_normalizer import (
    _looks_like_json_object,
    extract_first_json_dict,
    extract_json_object,
    iter_json_object_candidates,
    loads_safe,
)

# --- extract_json_object ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('  \n{"a": 1}\n ', '{"a": 1}'),
        ('Here you go: {"a": 1} done.', '{"a": 1}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('Result:\n```\n{"a": 1}\n```\nBye', '{"a": 1}'),
        ('{"a": {"b": [1, {"c": 2}]}}', '{"a": {"b": [1, {"c": 2}]}}'),
        # braces and quotes inside strings do not close the object early
        ('{"a": "}{"} tail', '{"a": "}{"}'),
        ('{"a": "say \\"}\\" now"} tail', '{"a": "say \\"}\\" now"}'),
        ("{}", "{}"),
    ],
)
def test_extract_json_object_finds_the_outermost_object(raw, expected):
    assert extract_json_object(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        None,
        "no braces here",
        '{"a": 1',  # never closed
        "{ just words }",  # braces without key/value shape
        "[1, 2, 3]",
    ],
)
def test_extract_json_object_returns_empty_when_nothing_usable(raw):
    assert extract_json_object(raw) == ""


def test_extract_json_object_prefers_a_fenced_block_over_earlier_text():
    raw = 'Draft {"draft": true}\n```json\n{"final": true}\n```'

    assert extract_json_object(raw) == '{"final": true}'


def test_extract_json_object_skips_an_unusable_fence_and_uses_the_text():
    raw = '```\nnot json\n```\n{"a": 1}'

    assert extract_json_object(raw) == '{"a": 1}'


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ('{"a": 1}', True),
        ("{}", True),
        ("{ }", True),
        ("{words}", False),
        ('{"only a string"}', False),
        ("a: 1", False),
        ('["a": 1]', False),
    ],
)
def test_looks_like_json_object(candidate, expected):
    assert _looks_like_json_object(candidate) is expected


# --- iter_json_object_candidates ---


def test_candidates_list_fenced_objects_before_bare_text_ones():
    raw = '{"bare": 1}\n```json\n{"fenced": 1}\n```'

    assert iter_json_object_candidates(raw) == ['{"fenced": 1}', '{"bare": 1}']


def test_candidates_are_unique_and_keep_order():
    raw = '{"a": 1} {"b": 2} {"a": 1}'

    assert iter_json_object_candidates(raw) == ['{"a": 1}', '{"b": 2}']


def test_candidates_dedupe_an_object_seen_in_fence_and_text():
    raw = '```json\n{"a": 1}\n```\nagain {"a": 1}'

    assert iter_json_object_candidates(raw) == ['{"a": 1}']


def test_candidates_return_each_top_level_object_not_nested_ones():
    raw = '{"a": {"inner": 1}} {"b": 2}'

    assert iter_json_object_candidates(raw) == ['{"a": {"inner": 1}}', '{"b": 2}']


@pytest.mark.parametrize("raw", ["", "  ", None, "plain text", "{ nope }"])
def test_candidates_are_empty_without_json_objects(raw):
    assert iter_json_object_candidates(raw) == []


# --- loads_safe ---


def test_loads_safe_reads_strict_json_directly():
    assert loads_safe('{"a": [1, 2]}') == {"a": [1, 2]}
    # a top level list is fine for the strict path
    assert loads_safe("[1, 2]") == [1, 2]


def test_loads_safe_pulls_the_object_out_of_surrounding_text():
    assert loads_safe('Sure! {"a": 1} Hope that helps.') == {"a": 1}


def test_loads_safe_reads_a_fenced_object():
    assert loads_safe('```json\n{"a": "x"}\n```') == {"a": "x"}


def test_loads_safe_repairs_raw_newline_and_tab_inside_a_string():
    raw = '{"text": "line one\nline two\tend"}'

    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)
    assert loads_safe(raw) == {"text": "line one\nline two\tend"}


def test_loads_safe_repairs_an_invalid_escape():
    raw = r'{"path": "C:\data\file"}'

    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)
    # \d is not a JSON escape and is kept literally (\f stays a valid escape)
    assert loads_safe(raw)["path"].startswith("C:\\data")


def test_loads_safe_drops_stray_control_characters_outside_strings():
    assert loads_safe('{"a":\x01 1}') == {"a": 1}


def test_loads_safe_strict_first_false_still_extracts_from_text():
    assert loads_safe('prefix {"a": 1} suffix', strict_first=False) == {"a": 1}


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_loads_safe_rejects_an_empty_payload(raw):
    with pytest.raises(json.JSONDecodeError):
        loads_safe(raw)


def test_loads_safe_raises_when_the_text_cannot_be_repaired():
    with pytest.raises(json.JSONDecodeError):
        loads_safe('{"a": }')


# --- extract_first_json_dict ---


def test_first_dict_returns_a_plain_object():
    assert extract_first_json_dict('{"a": 1}', error_context="x") == {"a": 1}


def test_first_dict_skips_a_broken_candidate():
    raw = '{"bad": } then {"good": 1}'

    assert extract_first_json_dict(raw, error_context="x") == {"good": 1}


def test_first_dict_takes_the_first_good_of_several():
    raw = 'a {"one": 1} b {"two": 2}'

    assert extract_first_json_dict(raw, error_context="x") == {"one": 1}


def test_first_dict_repairs_a_candidate_with_a_raw_newline_in_a_string():
    raw = 'note {"text": "a\nb"} end'

    assert extract_first_json_dict(raw, error_context="x") == {"text": "a\nb"}


@pytest.mark.parametrize(
    "raw", ["", "  ", None, "[1, 2]", "no json", '{"a": }', '"just a string"']
)
def test_first_dict_names_the_context_when_nothing_is_found(raw):
    with pytest.raises(ValueError, match="No JSON object found in capture phase"):
        extract_first_json_dict(raw, error_context="capture phase")
