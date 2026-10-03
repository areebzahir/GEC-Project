"""Variable matching: scorers and assignment (DESIGN.md section 14)."""

from __future__ import annotations

from research_normalizer.config import PipelineConfig
from research_normalizer.matching.assign import ColumnContext, match_variables
from research_normalizer.matching.scorers import score_pair
from research_normalizer.readme.variable_layouts import VariableDefinition
from research_normalizer.schema.document import ColumnType

CFG = PipelineConfig()


def test_exact():
    assert score_pair("CowID", "CowID").method == "exact"


def test_case_insensitive():
    r = score_pair("HP_Greenfeed", "HP_GreenFeed")
    assert r.method == "case_insensitive" and r.score >= 0.95


def test_normalized_separators():
    r = score_pair("milk-fat", "milk_fat")
    assert r.method in {"normalized", "case_insensitive"} and r.score >= 0.95


def test_abbreviation():
    r = score_pair("BWsmth_chg", "BWsmooth_chg")
    assert r.method == "token_abbreviation" and r.score >= 0.85


def test_jaro_winkler_false_positive_avoided():
    # milk_prt vs milk_fat must NOT be a high-confidence match (the Jaro-Winkler trap, Ref [24]).
    r = score_pair("milk_prt", "milk_fat")
    assert r.score < CFG.match_review_threshold


def _col(name, pos, ctype=ColumnType.STRING):
    return ColumnContext(name=name, position=pos, column_type=ctype,
                         distinct_values=frozenset(), distinct_count=2)


def _def(name, pos):
    return VariableDefinition(name=name, position=pos)


def test_greedy_keeps_exact_over_fuzzy():
    # BW_smooth (exact) must not be stolen by BWsmooth_chg's fuzzy pull.
    cols = [_col("BW_smooth", 0), _col("BWsmth_chg", 1)]
    defs = [_def("BW_smooth", 1), _def("BWsmooth_chg", 2)]
    out = match_variables(cols, defs, {}, CFG)
    assert out.matches["BW_smooth"].documented_name == "BW_smooth"
    assert out.matches["BWsmth_chg"].documented_name == "BWsmooth_chg"


def test_unmatched_documented_reported():
    cols = [_col("id", 0)]
    defs = [_def("id", 1), _def("ghost", 2)]
    out = match_variables(cols, defs, {}, CFG)
    assert [u.name for u in out.unmatched_documented] == ["ghost"]


def test_undocumented_column():
    cols = [_col("id", 0), _col("mystery", 1)]
    defs = [_def("id", 1)]
    out = match_variables(cols, defs, {}, CFG)
    assert out.matches["mystery"].status.value == "unmatched"
