"""README segmentation and abbreviation extraction (DESIGN.md section 11)."""

from __future__ import annotations

from research_normalizer.readme.abbreviations import extract_abbreviations
from research_normalizer.readme.segmenter import BlockType, segment


def test_allcaps_heading():
    secs = segment(["GENERAL INFORMATION", "Title: X"])
    assert any(s.title == "GENERAL INFORMATION" for s in secs)


def test_template_prompt_stripped_to_heading():
    # "Variable List: <prompt>" must become a heading, not a key/value.
    secs = segment(["Variable List: <list name, unit, labels>", "id, identifier", "age, years"])
    titles = [s.title for s in secs]
    assert any(t and "variable list" in t.lower() for t in titles)


def test_single_word_empty_value_is_keyvalue_not_heading():
    secs = segment(["Name: Jane", "ORCID:", "Institution: UofT"])
    kv_keys = [b.key for s in secs for b in s.blocks if b.type is BlockType.KEY_VALUE]
    assert "ORCID" in kv_keys  # stayed a (empty) key/value, did not become a section


def test_key_value_continuation_gathered():
    secs = segment(["Citation: Smith, J. 2020.", "   A long continuation line.", "Next: y"])
    values = {b.key: b.value for s in secs for b in s.blocks if b.type is BlockType.KEY_VALUE}
    assert "continuation" in values["Citation"]


def test_url_not_split_as_keyvalue():
    secs = segment(["See https://doi.org/10.5683/ABC for details"])
    # The line should not be parsed as key 'See https' / value.
    kv = [b for s in secs for b in s.blocks if b.type is BlockType.KEY_VALUE]
    assert not any("doi.org" in (b.value or "") and b.key.startswith("See") for b in kv)


def test_abbreviations():
    pairs = extract_abbreviations("Bacterial abundance (BA) was measured. Dissolved organic carbon (DOC).")
    assert pairs.get("BA", "").lower().endswith("abundance")
    assert "DOC" in pairs
