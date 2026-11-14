"""Phase 6 static checks of the semantic-model spec (bi/etl/semantic_model.py), before anything is built in Power BI.
A typo in a DAX column reference or a relationship would otherwise only show up as an error inside Power BI Desktop."""
import re

import pytest

import build_phase6_artifacts as art
import semantic_model as sm

COL_REF = re.compile(r"(?:'([^']+)'|\b([A-Za-z_][A-Za-z0-9_]*))\[([^\]]+)\]")
MEASURE_REF = re.compile(r"(?<![\w'\]])\[([^\]@]+)\]")
EXTERNAL = {"Threshold"}                     # created in Power BI (what-if parameter), not a view


@pytest.fixture(scope="module")
def view_columns(dev):
    rows = dev.cursor().execute("""SELECT v.name, c.name FROM sys.views v JOIN sys.columns c ON c.object_id = v.object_id
                                   WHERE SCHEMA_NAME(v.schema_id) = 'rpt'""").fetchall()
    out = {}
    for v, c in rows:
        out.setdefault(v, set()).add(c)
    return out


def _cols(table, view_columns):
    return view_columns[sm.TABLES[table].split(".", 1)[1]]


def test_every_rpt_view_is_a_model_table(view_columns):
    assert {v.split(".", 1)[1] for v in sm.TABLES.values()} == set(view_columns)


def test_relationship_columns_exist(view_columns):
    for a, b, c, d, _ in sm.RELATIONSHIPS:
        assert b in _cols(a, view_columns), f"{a}[{b}]"
        assert d in _cols(c, view_columns), f"{c}[{d}]"


def test_sort_by_columns_exist(view_columns):
    for t, c, by in sm.SORT_BY:
        assert {c, by} <= _cols(t, view_columns), (t, c, by)


def test_dax_column_references_exist(view_columns):
    texts = [(m.name, m.dax) for m in sm.MEASURES] + [(r, f"'{t}'" + e if e.startswith("[") else e) for r, t, e in sm.RLS_ROLES]
    for name, dax in texts:
        for quoted, bare, col in COL_REF.findall(dax):
            table = quoted or bare
            if table in EXTERNAL or col.startswith("@"):
                continue
            assert table in sm.TABLES, f"{name}: unknown table {table}"
            assert col in _cols(table, view_columns), f"{name}: {table}[{col}] does not exist"


def test_measure_references_are_defined():
    names = {m.name for m in sm.MEASURES} | {"Threshold Value"}
    for m in sm.MEASURES:
        stripped = COL_REF.sub("", m.dax)
        for ref in MEASURE_REF.findall(stripped):
            assert ref in names, f"{m.name} uses [{ref}], which is not a measure"


def test_names_unique_and_hand_measures_first_class():
    names = [m.name for m in sm.MEASURES]
    assert len(names) == len(set(names))
    assert {m.name for m in sm.MEASURES if m.hand} == {"Checks Evaluated", "Pass + Note", "DQ Pass Rate", "Predictions", "Correct Predictions",
                                                      "Accuracy", "Net P&L", "Cumulative Net P&L", "Net P&L FYTD"}


def test_tabular_editor_script_has_every_measure():
    script = art.add_measures_csx()
    for m in sm.MEASURES:
        assert art.cs(m.name) in script and art.cs(m.dax) in script


def test_every_sql_twin_runs(dev):
    for m in sm.MEASURES:
        for f in sm.cases_for(m):
            dev.cursor().execute(sm.measure_sql(m, f)).fetchall()


def test_sql_twins_reproduce_the_frozen_model_figures(dev):
    """The headline run: accuracy 0.4623, balanced accuracy and directional precision = the model's own CV figures."""
    case = [("ModelRun", "Model Run", sm.HEADLINE)]
    get = lambda name: float(dev.cursor().execute(sm.measure_sql(next(m for m in sm.MEASURES if m.name == name), case)).fetchone()[0])
    assert round(get("Accuracy"), 4) == 0.4623
    assert abs(get("Balanced Accuracy") - 0.36511290853731815) < 1e-9
    assert abs(get("Directional Precision") - 0.32272310162881396) < 1e-9
    assert get("Predictions") == 67583
