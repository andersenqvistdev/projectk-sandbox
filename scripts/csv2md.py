#!/usr/bin/env python3
"""csv2md — convert CSV into a GitHub-flavored Markdown table. Stdlib only."""

import argparse
import csv
import sys

ALIGN_MARKERS = {
    "left": ":---",
    "right": "---:",
    "center": ":---:",
    "none": "---",
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="csv2md",
        description="Convert a CSV file into a GitHub-flavored Markdown table.",
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="Path to the CSV file. Reads from stdin if omitted.",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help="Write the Markdown table to FILE instead of stdout.",
    )
    parser.add_argument(
        "--align",
        metavar="ALIGN",
        help="Comma-separated per-column alignment: left, right, or center "
        "(e.g. --align left,right,center). Unlisted columns default to "
        "no alignment.",
    )
    parser.add_argument(
        "--sort",
        metavar="COLUMN",
        help="Sort data rows by COLUMN (header name or 1-based column index).",
    )
    parser.add_argument(
        "--no-header",
        action="store_true",
        help="Treat the CSV as headerless; generates 'Column 1', 'Column 2', ... headers.",
    )
    return parser.parse_args(argv)


def read_rows(path):
    if path:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(csv.reader(f))
    sys.stdin.reconfigure(encoding="utf-8-sig")
    return list(csv.reader(sys.stdin))


def escape_cell(value):
    return value.replace("|", "\\|").replace("\n", " ").strip()


def resolve_column(column, header):
    if column in header:
        return header.index(column)
    try:
        index = int(column) - 1
    except ValueError:
        raise ValueError(f"unknown column: {column!r}")
    if index < 0 or index >= len(header):
        raise ValueError(f"column index out of range: {column!r}")
    return index


def parse_align(align, width):
    aligns = ["none"] * width
    for i, token in enumerate(a.strip().lower() for a in align.split(",")):
        if i >= width:
            break
        if token not in ALIGN_MARKERS:
            raise ValueError(f"unknown alignment: {token!r}")
        aligns[i] = token
    return aligns


def build_table(rows, no_header=False, align=None, sort=None):
    if not rows:
        return ""

    if no_header:
        width = len(rows[0])
        header = [f"Column {i + 1}" for i in range(width)]
        data = rows
    else:
        header = rows[0]
        data = rows[1:]

    width = len(header)

    if sort:
        idx = resolve_column(sort, header)
        data = sorted(data, key=lambda row: row[idx] if idx < len(row) else "")

    aligns = parse_align(align, width) if align else ["none"] * width

    lines = [
        "| " + " | ".join(escape_cell(c) for c in header) + " |",
        "| " + " | ".join(ALIGN_MARKERS[a] for a in aligns) + " |",
    ]
    for row in data:
        padded = (list(row) + [""] * width)[:width]
        lines.append("| " + " | ".join(escape_cell(c) for c in padded) + " |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    args = parse_args(argv)
    try:
        rows = read_rows(args.input)
        table = build_table(
            rows, no_header=args.no_header, align=args.align, sort=args.sort
        )
    except FileNotFoundError as e:
        print(f"csv2md: error: {e.strerror}: {e.filename}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"csv2md: error: {e}", file=sys.stderr)
        return 1

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(table)
    else:
        sys.stdout.write(table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
