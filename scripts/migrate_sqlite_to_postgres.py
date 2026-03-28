from __future__ import annotations

import argparse
from datetime import datetime
import json
from typing import Any

from sqlalchemy import MetaData, create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Migrate FionaTrade data from SQLite to PostgreSQL")
    p.add_argument("--sqlite-url", required=True, help="source sqlite url, e.g. sqlite:///./fionatrade.db")
    p.add_argument("--postgres-url", required=True, help="target postgres url, e.g. postgresql+psycopg://user:pass@host:5432/db")
    p.add_argument("--chunk-size", type=int, default=2000, help="rows per batch")
    p.add_argument("--truncate-target", action="store_true", help="truncate target tables before copy")
    p.add_argument(
        "--allow-active-writes",
        action="store_true",
        help="skip safety guard that blocks migration when live/backtest tasks are active",
    )
    p.add_argument("--skip-tables", default="", help="comma-separated table names to skip")
    return p.parse_args()


def _backend_from_url(url: str) -> str:
    lowered = url.lower()
    if lowered.startswith("sqlite"):
        return "sqlite"
    if lowered.startswith("postgresql") or lowered.startswith("postgres"):
        return "postgresql"
    return "unknown"


def _table_count(engine: Engine, table) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(table)).scalar_one())


def _truncate_tables(engine: Engine, table_names: list[str]) -> None:
    if not table_names:
        return
    quoted = ", ".join(f'"{name}"' for name in table_names)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))


def _serialize_row(row: dict[str, Any]) -> str:
    safe = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            safe[k] = v.isoformat()
        else:
            safe[k] = v
    return json.dumps(safe, sort_keys=True, ensure_ascii=False, default=str)


def _assert_source_quiet(source_engine: Engine, *, allow_active_writes: bool) -> None:
    if allow_active_writes:
        print("[migrate] WARN: --allow-active-writes enabled, skipping source activity guard")
        return

    inspector = inspect(source_engine)
    table_names = set(inspector.get_table_names())
    blockers: list[str] = []

    with source_engine.connect() as conn:
        if "worker_commands" in table_names:
            open_commands = int(
                conn.execute(
                    text("SELECT COUNT(*) FROM worker_commands WHERE status IN ('PENDING','RUNNING')")
                ).scalar_one()
            )
            if open_commands > 0:
                blockers.append(f"worker_commands open={open_commands}")

        if "worker_runs" in table_names:
            running_runs = int(
                conn.execute(
                    text("SELECT COUNT(*) FROM worker_runs WHERE status='RUNNING'")
                ).scalar_one()
            )
            if running_runs > 0:
                blockers.append(f"worker_runs running={running_runs}")

        if "backtest_runs" in table_names:
            running_backtests = int(
                conn.execute(
                    text("SELECT COUNT(*) FROM backtest_runs WHERE status='RUNNING'")
                ).scalar_one()
            )
            if running_backtests > 0:
                blockers.append(f"backtest_runs running={running_backtests}")

        if "runtime_controls" in table_names:
            row = conn.execute(
                text(
                    "SELECT value_json FROM runtime_controls "
                    "WHERE control_key='live_trading_enabled' LIMIT 1"
                )
            ).scalar_one_or_none()
            payload: dict[str, Any] = {}
            if isinstance(row, dict):
                payload = row
            elif isinstance(row, str) and row.strip():
                try:
                    parsed = json.loads(row)
                    if isinstance(parsed, dict):
                        payload = parsed
                except json.JSONDecodeError:
                    payload = {}
            if bool(payload.get("enabled")):
                blockers.append("runtime_controls live_trading_enabled=true")

    if blockers:
        joined = "; ".join(blockers)
        raise SystemExit(
            "source database appears active. stop worker/live/backtest first, "
            f"or rerun with --allow-active-writes. details: {joined}"
        )


def _copy_table(source_engine: Engine, target_engine: Engine, table, chunk_size: int) -> int:
    pk_cols = list(table.primary_key.columns)
    pk_col = pk_cols[0] if len(pk_cols) == 1 else None
    copied = 0

    if pk_col is not None:
        last_pk = None
        while True:
            stmt = select(table).order_by(pk_col.asc()).limit(chunk_size)
            if last_pk is not None:
                stmt = stmt.where(pk_col > last_pk)
            with source_engine.connect() as src_conn:
                rows = src_conn.execute(stmt).mappings().all()
            if not rows:
                break
            payload = [dict(r) for r in rows]
            with target_engine.begin() as dst_conn:
                dst_conn.execute(table.insert(), payload)
            copied += len(payload)
            last_pk = payload[-1][pk_col.name]
    else:
        with source_engine.connect() as src_conn:
            result = src_conn.execute(select(table)).mappings()
            while True:
                rows = result.fetchmany(chunk_size)
                if not rows:
                    break
                payload = [dict(r) for r in rows]
                with target_engine.begin() as dst_conn:
                    dst_conn.execute(table.insert(), payload)
                copied += len(payload)

    return copied


def _spot_check(source_engine: Engine, target_engine: Engine, table, sample: int = 3) -> tuple[bool, str]:
    pk_cols = list(table.primary_key.columns)
    if len(pk_cols) != 1:
        return True, "no_single_pk"
    pk = pk_cols[0]

    with source_engine.connect() as src_conn:
        src_rows = src_conn.execute(select(table).order_by(pk.asc()).limit(sample)).mappings().all()
    if not src_rows:
        return True, "empty"
    keys = [row[pk.name] for row in src_rows]

    with target_engine.connect() as dst_conn:
        dst_rows = dst_conn.execute(select(table).where(pk.in_(keys)).order_by(pk.asc())).mappings().all()

    if len(src_rows) != len(dst_rows):
        return False, f"sample_len_mismatch src={len(src_rows)} dst={len(dst_rows)}"

    src_map = {row[pk.name]: _serialize_row(dict(row)) for row in src_rows}
    dst_map = {row[pk.name]: _serialize_row(dict(row)) for row in dst_rows}
    for key in keys:
        if src_map.get(key) != dst_map.get(key):
            return False, f"row_mismatch pk={key}"
    return True, "ok"


def main() -> int:
    args = _parse_args()
    if _backend_from_url(args.sqlite_url) != "sqlite":
        raise SystemExit("--sqlite-url must point to sqlite backend")
    if _backend_from_url(args.postgres_url) != "postgresql":
        raise SystemExit("--postgres-url must point to postgresql backend")

    skip_tables = {x.strip() for x in args.skip_tables.split(",") if x.strip()}

    source_engine = create_engine(args.sqlite_url, future=True)
    target_engine = create_engine(args.postgres_url, future=True)
    _assert_source_quiet(source_engine, allow_active_writes=args.allow_active_writes)

    source_meta = MetaData()
    source_meta.reflect(bind=source_engine)

    if not source_meta.tables:
        raise SystemExit("source has no tables")

    # Create missing tables on target with reflected schema.
    source_meta.create_all(bind=target_engine, checkfirst=True)

    inspector = inspect(source_engine)
    table_names = [name for name in inspector.get_table_names() if name not in skip_tables]
    if not table_names:
        raise SystemExit("no tables selected for migration")

    print(f"[migrate] selected tables={len(table_names)} chunk_size={args.chunk_size}")

    if args.truncate_target:
        print("[migrate] truncating target tables...")
        _truncate_tables(target_engine, table_names)

    summary: list[dict[str, Any]] = []
    for name in table_names:
        table = source_meta.tables[name]
        src_count = _table_count(source_engine, table)
        dst_before = _table_count(target_engine, table)
        if dst_before > 0 and not args.truncate_target:
            raise SystemExit(
                f"target table '{name}' already has {dst_before} rows; rerun with --truncate-target or empty target"
            )

        print(f"[migrate] {name}: source={src_count} target_before={dst_before}")
        copied = _copy_table(source_engine, target_engine, table, args.chunk_size)
        dst_after = _table_count(target_engine, table)
        ok = src_count == dst_after
        sample_ok, sample_msg = _spot_check(source_engine, target_engine, table, sample=3)
        summary.append(
            {
                "table": name,
                "source": src_count,
                "copied": copied,
                "target_after": dst_after,
                "count_ok": ok,
                "sample_ok": sample_ok,
                "sample_msg": sample_msg,
            }
        )
        print(
            f"[migrate] {name}: copied={copied} target_after={dst_after} "
            f"count_ok={ok} sample_ok={sample_ok} ({sample_msg})"
        )

    failed = [row for row in summary if (not row["count_ok"] or not row["sample_ok"])]
    print("\n[migrate] summary")
    for row in summary:
        print(
            f"  - {row['table']}: src={row['source']} dst={row['target_after']} "
            f"count_ok={row['count_ok']} sample_ok={row['sample_ok']}"
        )

    if failed:
        print(f"\n[migrate] FAILED tables={len(failed)}")
        return 2

    print("\n[migrate] SUCCESS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
