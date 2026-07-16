"""QA regression suite for csv2md G3: --align (left/right/center) and --sort.

Focused on the alignment/sort contract from .company/vision.md:
"alignment markers appear in the separator row and --sort reorders rows
correctly." Complements (does not duplicate) cli-developer's own unit tests —
this suite leans on CLI-level (subprocess) execution and adversarial edge
cases: whitespace/case in --align tokens, ragged rows, stability, sort-key
resolution edge cases, and combined --align + --sort usage.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "csv2md.py"

sys.path.insert(0, str(ROOT / "scripts"))
import csv2md  # noqa: E402


def run(args, input=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=input,
        capture_output=True,
        text=True,
    )


def write_csv(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


# --- --align: separator row correctness (CLI level) ---


def test_cli_align_all_three_markers(tmp_path):
    csv_path = write_csv(tmp_path, "in.csv", "name,age,city\nAda,36,London\n")
    result = run([str(csv_path), "--align", "left,right,center"])
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert lines[1] == "| :--- | ---: | :---: |"


def test_cli_align_none_marker_is_default_for_unlisted_columns(tmp_path):
    csv_path = write_csv(tmp_path, "in.csv", "a,b,c\n1,2,3\n")
    result = run([str(csv_path), "--align", "center"])
    lines = result.stdout.splitlines()
    assert lines[1] == "| :---: | --- | --- |"


def test_cli_no_align_flag_uses_plain_separator(tmp_path):
    csv_path = write_csv(tmp_path, "in.csv", "a,b\n1,2\n")
    result = run([str(csv_path)])
    lines = result.stdout.splitlines()
    assert lines[1] == "| --- | --- |"


# --- --align: token parsing edge cases ---


def test_align_tokens_are_case_insensitive():
    table = csv2md.build_table(
        [["a", "b"], ["1", "2"]],
        align="LEFT,Right",
    )
    assert table.splitlines()[1] == "| :--- | ---: |"


def test_align_tokens_tolerate_surrounding_whitespace():
    table = csv2md.build_table(
        [["a", "b"], ["1", "2"]],
        align=" left , right ",
    )
    assert table.splitlines()[1] == "| :--- | ---: |"


def test_align_explicit_none_token_is_accepted():
    table = csv2md.build_table(
        [["a", "b"], ["1", "2"]],
        align="none,left",
    )
    assert table.splitlines()[1] == "| --- | :--- |"


def test_align_extra_tokens_beyond_header_width_are_ignored():
    table = csv2md.build_table(
        [["a", "b"], ["1", "2"]],
        align="left,right,center,center",
    )
    lines = table.splitlines()
    assert lines[1] == "| :--- | ---: |"
    assert lines[1].count("|") == 3  # 2 columns -> 3 pipe delimiters


def test_align_single_column_table():
    table = csv2md.build_table([["only"], ["x"]], align="center")
    assert table.splitlines()[1] == "| :---: |"


def test_align_separator_column_count_matches_header_even_with_ragged_data():
    table = csv2md.build_table(
        [["a", "b", "c"], ["1"], ["2", "3", "4", "5"]],
        align="left",
    )
    lines = table.splitlines()
    assert lines[1] == "| :--- | --- | --- |"
    # every data row is padded/truncated to header width, not its own width
    assert lines[2] == "| 1 |  |  |"
    assert lines[3] == "| 2 | 3 | 4 |"


def test_align_unknown_token_error_message_names_the_bad_token():
    with pytest.raises(ValueError, match="middle"):
        csv2md.build_table([["a"], ["1"]], align="middle")


def test_cli_align_unknown_token_exits_nonzero_with_stderr(tmp_path):
    csv_path = write_csv(tmp_path, "in.csv", "a\n1\n")
    result = run([str(csv_path), "--align", "diagonal"])
    assert result.returncode == 1
    assert result.stdout == ""
    assert "error" in result.stderr


# --- --sort: correctness and key resolution ---


def test_cli_sort_by_header_name_reorders_data_rows_only(tmp_path):
    csv_path = write_csv(tmp_path, "in.csv", "name,age\nLinus,54\nAda,36\nGrace,30\n")
    result = run([str(csv_path), "--sort", "name"])
    lines = result.stdout.splitlines()
    assert lines[0] == "| name | age |"  # header untouched
    assert lines[2:] == ["| Ada | 36 |", "| Grace | 30 |", "| Linus | 54 |"]


def test_sort_is_stable_for_equal_keys():
    table = csv2md.build_table(
        [
            ["name", "team"],
            ["Ada", "red"],
            ["Bob", "blue"],
            ["Cara", "red"],
        ],
        sort="team",
    )
    lines = table.splitlines()[2:]
    # both "red" rows keep their original relative order after "blue"
    red_rows = [line for line in lines if line.endswith("| red |")]
    assert red_rows == ["| Ada | red |", "| Cara | red |"]


def test_sort_by_one_based_column_index():
    table = csv2md.build_table(
        [["name", "age"], ["Linus", "54"], ["Ada", "36"]],
        sort="2",
    )
    lines = table.splitlines()
    assert lines[2] == "| Ada | 36 |"
    assert lines[3] == "| Linus | 54 |"


def test_sort_column_index_zero_is_out_of_range():
    with pytest.raises(ValueError):
        csv2md.build_table([["a", "b"], ["1", "2"]], sort="0")


def test_sort_column_index_beyond_width_raises():
    with pytest.raises(ValueError):
        csv2md.build_table([["a", "b"], ["1", "2"]], sort="99")


def test_sort_unknown_header_name_raises_with_column_in_message():
    with pytest.raises(ValueError, match="nope"):
        csv2md.build_table([["a"], ["1"]], sort="nope")


def test_sort_treats_values_lexicographically_not_numerically():
    """Documents current behavior: sort is a plain string compare, so
    numeric-looking columns do NOT sort in numeric order (e.g. "10" sorts
    before "9"). This is a known limitation, not a crash — see QA bug note.
    """
    table = csv2md.build_table(
        [["n"], ["9"], ["10"], ["2"]],
        sort="n",
    )
    lines = table.splitlines()[2:]
    assert lines == ["| 10 |", "| 2 |", "| 9 |"]


def test_sort_missing_value_in_ragged_row_sorts_as_empty_string():
    table = csv2md.build_table(
        [["a", "b"], ["z", "1"], ["a"]],  # second row missing column b
        sort="b",
    )
    lines = table.splitlines()[2:]
    assert lines[0] == "| a |  |"  # empty "" sorts first
    assert lines[1] == "| z | 1 |"


def test_sort_with_no_header_uses_generated_column_name():
    table = csv2md.build_table(
        [["9"], ["1"], ["5"]],
        no_header=True,
        sort="Column 1",
    )
    lines = table.splitlines()
    assert lines[0] == "| Column 1 |"
    assert lines[2:] == ["| 1 |", "| 5 |", "| 9 |"]


# --- --align and --sort combined ---


def test_cli_align_and_sort_combined(tmp_path):
    csv_path = write_csv(tmp_path, "in.csv", "name,age\nLinus,54\nAda,36\n")
    result = run([str(csv_path), "--align", "left,right", "--sort", "name"])
    lines = result.stdout.splitlines()
    assert lines[1] == "| :--- | ---: |"
    assert lines[2:] == ["| Ada | 36 |", "| Linus | 54 |"]


def test_sort_does_not_disturb_header_row_or_alignment_row_order():
    table = csv2md.build_table(
        [["b", "a"], ["2", "y"], ["1", "x"]],
        align="right,left",
        sort="a",
    )
    lines = table.splitlines()
    assert lines[0] == "| b | a |"
    assert lines[1] == "| ---: | :--- |"
    assert lines[2:] == ["| 1 | x |", "| 2 | y |"]


# --- --align: further token-parsing edge cases ---


def test_align_trailing_comma_yields_empty_token_and_raises():
    """A trailing comma (e.g. --align left,) produces an empty token that
    is not a valid alignment keyword and must fail loudly rather than being
    silently ignored like tokens past the header width are."""
    with pytest.raises(ValueError, match="''"):
        csv2md.build_table([["a", "b"], ["1", "2"]], align="left,")


def test_cli_align_trailing_comma_exits_nonzero_with_stderr(tmp_path):
    csv_path = write_csv(tmp_path, "in.csv", "a,b\n1,2\n")
    result = run([str(csv_path), "--align", "left,"])
    assert result.returncode == 1
    assert result.stdout == ""
    assert "error" in result.stderr


def test_align_empty_string_behaves_as_no_alignment_flag():
    """--align "" is falsy in Python, so build_table takes the same branch
    as "no --align given at all" rather than raising on an empty token.
    This documents that quirk of the current implementation."""
    with_empty = csv2md.build_table([["a", "b"], ["1", "2"]], align="")
    without_flag = csv2md.build_table([["a", "b"], ["1", "2"]], align=None)
    assert with_empty == without_flag
    assert with_empty.splitlines()[1] == "| --- | --- |"


def test_align_header_only_table_still_emits_separator_row():
    table = csv2md.build_table([["a", "b"]], align="left,right")
    lines = table.splitlines()
    assert lines == ["| a | b |", "| :--- | ---: |"]


# --- --sort: column resolution edge cases ---


def test_sort_prefers_header_name_match_over_numeric_column_index():
    """When a header is literally named "1", resolve_column must match it
    by name rather than reinterpreting "1" as a 1-based column index."""
    table = csv2md.build_table(
        [["1", "b"], ["z", "2"], ["a", "1"]],
        sort="1",
    )
    lines = table.splitlines()[2:]
    assert lines == ["| a | 1 |", "| z | 2 |"]


def test_sort_duplicate_header_name_resolves_to_first_occurrence():
    table = csv2md.build_table(
        [["a", "a"], ["2", "x"], ["1", "y"]],
        sort="a",
    )
    lines = table.splitlines()[2:]
    assert lines == ["| 1 | y |", "| 2 | x |"]
