from __future__ import annotations

import argparse
import sys

from csvtool.filtering import parse_where, select_fields


def read_rows(path):
    text = open(path, encoding="utf-8").read().strip()
    lines = text.splitlines()
    headers = lines[0].split(",")
    rows = []
    for line in lines[1:]:
        values = line.split(",")
        rows.append(dict(zip(headers, values)))
    return headers, rows


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_file")
    parser.add_argument("--where")
    parser.add_argument("--select")
    parser.add_argument("--sort")
    args = parser.parse_args(argv)

    headers, rows = read_rows(args.csv_file)
    predicate = parse_where(args.where)
    rows = [row for row in rows if predicate(row)]
    if args.sort:
        rows.sort(key=lambda row: row.get(args.sort, ""))
    rows = select_fields(rows, args.select)
    if rows:
        headers = list(rows[0].keys())
    print(",".join(headers))
    for row in rows:
        print(",".join(str(row.get(header, "")) for header in headers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
