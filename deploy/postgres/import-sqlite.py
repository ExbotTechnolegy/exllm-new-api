#!/usr/bin/env python3
"""Copy an offline New API SQLite snapshot into an initialized PostgreSQL schema."""
import argparse
from decimal import Decimal
import os
from pathlib import Path
import sqlite3
import sys

import psycopg
from psycopg import sql


ACCOUNTING_FIELDS = {
    "users": ("id", "quota", "used_quota"),
    "tokens": ("id", "user_id", "remain_quota", "used_quota"),
    "top_ups": ("id", "user_id", "trade_no", "amount", "money", "status"),
    "logs": ("id", "user_id", "model_name", "quota", "type"),
    "user_subscriptions": ("id", "user_id", "amount_total", "amount_used", "status"),
    "subscription_orders": ("id", "user_id", "trade_no", "money", "status"),
}


def sqlite_id(name):
    return '"' + name.replace('"', '""') + '"'


def compare_accounting(source, target, table, names):
    source_columns = ", ".join(map(sqlite_id, names))
    source_query = f"SELECT {source_columns} FROM {sqlite_id(table)} ORDER BY id"
    target_query = sql.SQL("SELECT {} FROM {} ORDER BY id").format(
        sql.SQL(", ").join(map(sql.Identifier, names)), sql.Identifier("public", table)
    )
    with target.cursor(name="check_accounting") as cursor:
        cursor.execute(target_query)
        for original, imported in zip(source.execute(source_query), cursor, strict=True):
            # SQLite REAL is a float; PostgreSQL NUMERIC preserves its decimal text.
            comparable = tuple(
                Decimal(str(old)) if isinstance(old, float) and isinstance(new, Decimal) else old
                for old, new in zip(original, imported, strict=True)
            )
            if comparable != imported:
                raise ValueError(f"Accounting fields differ in {table}")


def copy_snapshot(source, target, args):
    db_name = target.execute("SELECT current_database()").fetchone()[0]
    if db_name != args.target_db:
        raise ValueError("Connected database does not match --target-db")

    source_tables = [
        row[0] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    if not {"users", "tokens", "top_ups", "logs"} <= set(source_tables):
        raise ValueError("Source does not contain the expected New API tables")

    pg_tables = {row[0] for row in target.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'"
    )}
    if set(source_tables) - pg_tables:
        raise ValueError(f"Target schema lacks tables: {', '.join(sorted(set(source_tables) - pg_tables))}")
    sqlite_sequences = {}
    if source.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
        sqlite_sequences = dict(source.execute("SELECT name, seq FROM sqlite_sequence"))

    plans = {}
    for table in source_tables:
        columns = [row[1] for row in source.execute(f"PRAGMA table_info({sqlite_id(table)})")]
        target_types = dict(target.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s", (table,),
        ))
        extra = set(columns) - target_types.keys()
        if extra:
            raise ValueError(f"Target schema lacks columns in {table}: {', '.join(sorted(extra))}")
        plans[table] = (columns, [i for i, name in enumerate(columns) if target_types[name] == "boolean"])

    tables = sql.SQL(", ").join(sql.Identifier("public", table) for table in sorted(pg_tables))
    target.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(tables))
    nonempty = [
        table for table in sorted(pg_tables)
        if target.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier("public", table))).fetchone()
    ]
    if nonempty and not args.replace_bootstrap:
        raise ValueError("Target already has records. Use a new database or explicitly confirm the isolated bootstrap target.")
    untouched = set(nonempty) - set(source_tables)
    if untouched:
        raise ValueError(f"Target-only tables contain records: {', '.join(sorted(untouched))}. Review before importing.")
    if nonempty:
        copied_tables = sql.SQL(", ").join(sql.Identifier("public", table) for table in source_tables)
        target.execute(sql.SQL("TRUNCATE TABLE {}").format(copied_tables))

    for table, (columns, bool_indexes) in plans.items():
        identifier = sql.Identifier("public", table)
        copy = sql.SQL("COPY {} ({}) FROM STDIN").format(identifier, sql.SQL(", ").join(map(sql.Identifier, columns)))
        source_columns = ", ".join(map(sqlite_id, columns))
        copied = 0
        try:
            with target.cursor().copy(copy) as writer:
                for original in source.execute(f"SELECT {source_columns} FROM {sqlite_id(table)}"):
                    row = list(original)
                    for index in bool_indexes:
                        if row[index] is not None:
                            if row[index] not in (0, 1):
                                raise ValueError("unexpected SQLite boolean")
                            row[index] = bool(row[index])
                    writer.write_row(row)
                    copied += 1
        except (psycopg.Error, ValueError) as exc:
            raise ValueError(f"Copy failed in {table} near row {copied + 1} ({getattr(exc, 'sqlstate', None) or type(exc).__name__})") from None

        count = target.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(identifier)).fetchone()[0]
        if copied != count:
            raise ValueError(f"Row count mismatch in {table}")
        if table in ACCOUNTING_FIELDS:
            compare_accounting(source, target, table, ACCOUNTING_FIELDS[table])

        for column in columns:
            sequence = target.execute(
                "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE c.oid=pg_get_serial_sequence(%s, %s)::regclass",
                (identifier.as_string(target), column),
            ).fetchone()
            if sequence:
                maximum = target.execute(sql.SQL("SELECT COALESCE(MAX({}), 0) FROM {}").format(
                    sql.Identifier(column), identifier
                )).fetchone()[0]
                next_value = max(maximum, sqlite_sequences.get(table, 0)) + 1
                target.execute(sql.SQL("ALTER SEQUENCE {} RESTART WITH {}").format(
                    sql.Identifier(*sequence), sql.Literal(next_value)
                ))
        print(f"{table}: {count} rows")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--target-db", required=True)
    parser.add_argument("--replace-bootstrap", action="store_true", help="replace rows in an isolated bootstrap database")
    parser.add_argument("--apply", action="store_true", help="commit; default runs the checks and rolls back")
    args = parser.parse_args()
    if not os.environ.get("EXAI_SQL_DSN"):
        parser.error("EXAI_SQL_DSN is required")
    snapshot = args.snapshot.expanduser().resolve()
    if not snapshot.is_file():
        parser.error("snapshot must be an existing regular file")
    if Path(str(snapshot) + "-wal").exists():
        parser.error("use a standalone SQLite backup, not a database with a WAL sidecar")
    with sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True) as source:
        source.execute("BEGIN")
        if source.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite snapshot did not pass quick_check")
        with psycopg.connect(os.environ["EXAI_SQL_DSN"]) as target:
            copy_snapshot(source, target, args)
            if not args.apply:
                target.rollback()
    print("Import committed." if args.apply else "Dry run passed. PostgreSQL transaction rolled back.")


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        sys.exit(str(exc))
    except (psycopg.Error, sqlite3.Error, OSError) as exc:
        # Driver messages may include credentials or copied row values.
        sys.exit(f"Migration failed ({getattr(exc, 'sqlstate', None) or type(exc).__name__}).")
