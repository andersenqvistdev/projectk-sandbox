"""QA coverage sprint for csv2md G4 (coverage above 80%).

Complements tests/test_csv2md.py, which drives csv2md.py mostly through
subprocess (real CLI behavior, but invisible to coverage.py since it runs
in a child process) and direct csv2md.build_table calls. This suite calls
parse_args, read_rows, and main directly in-process to close the coverage
gap on argument parsing, stdin/file reading, and main()'s success/error
branches, plus a couple of resolve_column/parse_align edge cases the
existing suite doesn't reach.
"""

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "scripts"))
import csv2md  # noqa: E402


def write_csv(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


# --- parse_args ---


def test_parse_args_defaults():
    args = csv2md.parse_args(["in.csv"])
    assert args.input == "in.csv"
    assert args.output is None
    assert args.align is None
    assert args.sort is None
    assert args.no_header is False


def test_parse_args_no_input_defaults_to_none():
    args = csv2md.parse_args([])
    assert args.input is None


def test_parse_args_all_flags():
    args = csv2md.parse_args(
        [
            "in.csv",
            "-o",
            "out.md",
            "--align",
            "left,right",
            "--sort",
            "name",
            "--no-header",
        ]
    )
    assert args.input == "in.csv"
    assert args.output == "out.md"
    assert args.align == "left,right"
    assert args.sort == "name"
    assert args.no_header is True


# --- read_rows ---


def test_read_rows_from_file(tmp_path):
    csv_path = write_csv(tmp_path, "in.csv", "a,b\n1,2\n")
    assert csv2md.read_rows(str(csv_path)) == [["a", "b"], ["1", "2"]]


def test_read_rows_from_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("a,b\n1,2\n"))
    assert csv2md.read_rows(None) == [["a", "b"], ["1", "2"]]


def test_read_rows_from_empty_path_arg_reads_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert csv2md.read_rows("") == []


# --- main(): success paths ---


def test_main_writes_to_stdout(tmp_path, capsys):
    csv_path = write_csv(tmp_path, "in.csv", "name,age\nAda,36\n")
    exit_code = csv2md.main([str(csv_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "| name | age |\n| --- | --- |\n| Ada | 36 |\n"
    assert captured.err == ""


def test_main_writes_to_output_file(tmp_path, capsys):
    csv_path = write_csv(tmp_path, "in.csv", "a,b\n1,2\n")
    out_path = tmp_path / "out.md"
    exit_code = csv2md.main([str(csv_path), "-o", str(out_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert (
        out_path.read_text(encoding="utf-8") == "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    )


def test_main_reads_stdin_when_no_input_given(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("a,b\n1,2\n"))
    exit_code = csv2md.main([])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "| 1 | 2 |" in captured.out


def test_main_applies_align_sort_and_no_header(tmp_path, capsys):
    csv_path = write_csv(tmp_path, "in.csv", "Bob,20\nAda,36\n")
    exit_code = csv2md.main(
        [str(csv_path), "--no-header", "--align", "left,right", "--sort", "1"]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    lines = captured.out.splitlines()
    assert lines[0] == "| Column 1 | Column 2 |"
    assert lines[1] == "| :--- | ---: |"
    assert lines[2:] == ["| Ada | 36 |", "| Bob | 20 |"]


# --- main(): error paths ---


def test_main_missing_file_returns_1_and_reports_stderr(capsys):
    exit_code = csv2md.main(["does-not-exist.csv"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "does-not-exist.csv" in captured.err
    assert "error" in captured.err


def test_main_invalid_align_returns_1(tmp_path, capsys):
    csv_path = write_csv(tmp_path, "in.csv", "a\n1\n")
    exit_code = csv2md.main([str(csv_path), "--align", "bogus"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "error" in captured.err
    assert "bogus" in captured.err


def test_main_invalid_sort_column_returns_1(tmp_path, capsys):
    csv_path = write_csv(tmp_path, "in.csv", "a,b\n1,2\n")
    exit_code = csv2md.main([str(csv_path), "--sort", "nope"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error" in captured.err
    assert "nope" in captured.err


# --- resolve_column / parse_align edge cases not reached by the CLI suite ---


def test_resolve_column_numeric_index_out_of_range():
    with pytest.raises(ValueError, match="99"):
        csv2md.build_table([["a", "b"], ["1", "2"]], sort="99")


def test_parse_align_stops_at_header_width():
    table = csv2md.build_table(
        [["a", "b"], ["1", "2"]],
        align="left,right,center,center",
    )
    lines = table.splitlines()
    assert lines[1] == "| :--- | ---: |"
