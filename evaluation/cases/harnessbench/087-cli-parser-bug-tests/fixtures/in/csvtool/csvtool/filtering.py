from __future__ import annotations


def parse_where(expr: str | None):
    if not expr:
        return lambda row: True
    field, value = expr.split("=")
    return lambda row: row.get(field) == value


def select_fields(rows, fields):
    if not fields:
        return rows
    names = fields.split(",")
    return [{name: row.get(name, "") for name in names} for row in rows]
