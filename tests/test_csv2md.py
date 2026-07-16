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


# --- core conversion (G1) ---


def test_basic_file_conversion(tmp_path):
    csv_path = write_csv(tmp_path, "in.csv", "name,age\nAda,36\nLinus,54\n")
    result = run([str(csv_path)])
    assert result.returncode == 0
    assert result.stdout == (
        "| name | age |\n| --- | --- |\n| Ada | 36 |\n| Linus | 54 |\n"
    )


def test_escapes_pipe_and_newline_in_cells():
    table = csv2md.build_table([["a|b", "c"], ["x\ny", "z"]])
    assert "a\\|b" in table
    assert "x y" in table


def test_missing_file_reports_error():
    result = run(["does-not-exist.csv"])
    assert result.returncode == 1
    assert "error" in result.stderr


def test_strips_utf8_bom_from_file_header(tmp_path):
    csv_path = tmp_path / "in.csv"
    csv_path.write_bytes(b"\xef\xbb\xbfname,age\nAda,36\n")
    result = run([str(csv_path)])
    assert result.returncode == 0
    assert result.stdout == "| name | age |\n| --- | --- |\n| Ada | 36 |\n"


def test_strips_utf8_bom_from_stdin_header():
    result = run([], input="﻿name,age\nAda,36\n")
    assert result.returncode == 0
    assert result.stdout == "| name | age |\n| --- | --- |\n| Ada | 36 |\n"


# --- stdin + output file (G2) ---


def test_reads_from_stdin():
    result = run([], input="name,age\nAda,36\n")
    assert result.returncode == 0
    assert "| Ada | 36 |" in result.stdout


def test_writes_to_output_file(tmp_path):
    csv_path = write_csv(tmp_path, "in.csv", "a,b\n1,2\n")
    out_path = tmp_path / "out.md"
    result = run([str(csv_path), "-o", str(out_path)])
    assert result.returncode == 0
    assert result.stdout == ""
    assert out_path.read_text(encoding="utf-8") == (
        "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    )


# --- alignment + sort (G3) ---


def test_align_markers():
    table = csv2md.build_table(
        [["name", "age", "city"], ["Ada", "36", "London"]],
        align="left,right,center",
    )
    assert table.splitlines()[1] == "| :--- | ---: | :---: |"


def test_align_partial_defaults_remaining_to_none():
    table = csv2md.build_table(
        [["a", "b"], ["1", "2"]],
        align="left",
    )
    assert table.splitlines()[1] == "| :--- | --- |"


def test_unknown_align_raises():
    with pytest.raises(ValueError):
        csv2md.build_table([["a"], ["1"]], align="middle")


def test_sort_by_header_name():
    table = csv2md.build_table(
        [["name", "age"], ["Linus", "54"], ["Ada", "36"]],
        sort="name",
    )
    lines = table.splitlines()
    assert lines[2] == "| Ada | 36 |"
    assert lines[3] == "| Linus | 54 |"


def test_sort_by_column_index():
    table = csv2md.build_table(
        [["name", "age"], ["Linus", "54"], ["Ada", "36"]],
        sort="2",
    )
    lines = table.splitlines()
    assert lines[2] == "| Ada | 36 |"
    assert lines[3] == "| Linus | 54 |"


def test_sort_unknown_column_raises():
    with pytest.raises(ValueError):
        csv2md.build_table([["a"], ["1"]], sort="nope")


def test_no_header_generates_column_names():
    table = csv2md.build_table([["1", "2"]], no_header=True)
    assert table.splitlines()[0] == "| Column 1 | Column 2 |"


# --- edge cases (G4) ---


def test_empty_input_returns_empty_string():
    assert csv2md.build_table([]) == ""


def test_ragged_rows_are_padded():
    table = csv2md.build_table([["a", "b", "c"], ["1"], ["2", "3", "4", "5"]])
    lines = table.splitlines()
    assert lines[2] == "| 1 |  |  |"
    assert lines[3] == "| 2 | 3 | 4 |"
