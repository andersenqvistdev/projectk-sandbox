# csv2md

A single-file Python CLI that converts CSV into a GitHub-flavored Markdown
table. Stdlib only — no dependencies to install. Reads a CSV from a file or
stdin and writes an aligned Markdown table to stdout or a file.

## Install

No packaging step required — `csv2md` is one script.

```bash
git clone https://github.com/andersenqvistdev/projectk-sandbox.git
cd projectk-sandbox
chmod +x scripts/csv2md.py
```

Run it directly with Python 3:

```bash
python3 scripts/csv2md.py --help
```

Or put it on your `PATH` for a shorter command:

```bash
ln -s "$(pwd)/scripts/csv2md.py" /usr/local/bin/csv2md
csv2md --help
```

The rest of this document uses `csv2md` as shorthand for
`python3 scripts/csv2md.py`.

## Usage

```
csv2md [-h] [-o FILE] [--align ALIGN] [--sort COLUMN] [--no-header] [input]
```

All examples below use this sample file:

```bash
cat > sample.csv <<'EOF'
name,age,city
Ada,36,London
Linus,54,Helsinki
Grace,85,New York
EOF
```

### Basic conversion

Convert a CSV file to a Markdown table on stdout:

```bash
csv2md sample.csv
```

```
| name | age | city |
| --- | --- | --- |
| Ada | 36 | London |
| Linus | 54 | Helsinki |
| Grace | 85 | New York |
```

## Flags

### `input` (positional, optional)

Path to the CSV file to convert. Omit it to read from stdin.

```bash
cat sample.csv | csv2md
```

```
| name | age | city |
| --- | --- | --- |
| Ada | 36 | London |
| Linus | 54 | Helsinki |
| Grace | 85 | New York |
```

### `-o FILE`, `--output FILE`

Write the Markdown table to `FILE` instead of stdout.

```bash
csv2md sample.csv -o table.md
cat table.md
```

```
| name | age | city |
| --- | --- | --- |
| Ada | 36 | London |
| Linus | 54 | Helsinki |
| Grace | 85 | New York |
```

### `--align ALIGN`

Comma-separated per-column alignment: `left`, `right`, or `center`. Columns
not listed keep the default (no alignment marker). The list is positional —
the first value aligns the first column, and so on.

```bash
csv2md sample.csv --align left,right,center
```

```
| name | age | city |
| :--- | ---: | :---: |
| Ada | 36 | London |
| Linus | 54 | Helsinki |
| Grace | 85 | New York |
```

### `--sort COLUMN`

Sort data rows by `COLUMN`. Accepts either a header name or a 1-based column
index; the header row itself is never moved.

```bash
csv2md sample.csv --sort name
```

```
| name | age | city |
| --- | --- | --- |
| Ada | 36 | London |
| Grace | 85 | New York |
| Linus | 54 | Helsinki |
```

Equivalent using a column index (`name` is column 1):

```bash
csv2md sample.csv --sort 1
```

### `--no-header`

Treat the CSV as headerless: every row is data, and columns are labeled
`Column 1`, `Column 2`, etc.

```bash
printf '1,2,3\n4,5,6\n' | csv2md --no-header
```

```
| Column 1 | Column 2 | Column 3 |
| --- | --- | --- |
| 1 | 2 | 3 |
| 4 | 5 | 6 |
```

### `-h`, `--help`

Show usage and exit.

```bash
csv2md --help
```

## Combining flags

Flags compose freely. Sort by age (descending sort isn't built in, so sort
ascending and read bottom-up, or pipe through your own reversal) and
right-align the numeric column:

```bash
csv2md sample.csv --sort age --align left,right,center -o sorted.md
```

## Edge cases

- **Empty input** — an empty file (or empty stdin) produces empty output.
- **Ragged rows** — rows with fewer columns than the header are padded with
  empty cells; rows with more columns are truncated to the header width.
- **Pipes and newlines in cells** — `|` is escaped to `\|` and embedded
  newlines are collapsed to a single space so the table stays valid Markdown.
- **Missing input file** — `csv2md` exits with status `1` and prints an
  error to stderr, e.g. `csv2md: error: No such file or directory: nope.csv`.

## Tests

```bash
pytest tests/
```
