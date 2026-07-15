# Vision — csv2md

**Product:** `csv2md` — a single-file Python CLI that converts CSV files into
GitHub-flavored Markdown tables. Stdlib only. Reads a CSV (file or stdin),
emits an aligned Markdown table (or stdout), with options for column
alignment, header handling, and sorting.

Single file `scripts/csv2md.py`; tests in `tests/test_csv2md.py`.

## Goals

| Goal | Description | Deliverable | Done when | Owner |
|------|-------------|-------------|-----------|-------|
| G1 | Core conversion | `scripts/csv2md.py` reads a CSV file and prints a valid GitHub-flavored Markdown table to stdout | `csv2md input.csv` emits an aligned pipe table with a header separator row | cli-developer |
| G2 | Stdin + output file | Read CSV from stdin when no file given; write to a file with `-o/--output` | `cat x.csv \| csv2md` works and `csv2md x.csv -o out.md` writes the table | cli-developer |
| G3 | Column alignment + sort | `--align left,right,center` per column and `--sort COL` to sort rows by a column | alignment markers appear in the separator row; `--sort` reorders rows correctly | cli-developer |
| G4 | Test coverage >80% | `tests/test_csv2md.py` covering conversion, stdin, alignment, sort, and edge cases (empty, ragged rows) | pytest passes with coverage >80% on csv2md.py | qa-engineer |
| G5 | README | `README.md` documenting install, usage, all flags, and examples | README present with a runnable example for every flag | tech-writer |
