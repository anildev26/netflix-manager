#!/usr/bin/env python3
"""Operator tool for backup, restore, validation, and rollback deltas.

Database URLs come only from SOURCE_DATABASE_URL and TARGET_DATABASE_URL.
The process refuses URLs placed on the command line. It prints host, port,
and database name, never the user or password, and never customer fields.

Nothing is committed unless --commit and --write-window-acknowledged
writes-frozen are both present. A dry-run rolls the target transaction back.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from netflix_manager.transfer import (  # noqa: E402
    TransferError,
    apply_delta,
    apply_migrations,
    backup,
    connect,
    diff,
    identity,
    restore,
    validate,
)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if any("://" in arg or "postgres" in arg.lower() for arg in argv):
        print("Pass database URLs only through environment variables.", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="Netflix Manager database transfer")
    parser.add_argument("command", choices=("backup", "restore", "validate", "dry-run", "diff", "apply-delta", "apply-schema"))
    parser.add_argument("--out")
    parser.add_argument("--file")
    parser.add_argument("--confirm-source")
    parser.add_argument("--confirm-target")
    parser.add_argument("--confirm-onto")
    parser.add_argument("--write-window-acknowledged")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--ignore-extra-tables", action="store_true")
    parser.add_argument("--propagate-deletes", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            return _backup(args)
        if args.command == "restore":
            return _restore(args)
        if args.command == "validate":
            return _validate(args)
        if args.command == "dry-run":
            return _dry_run(args)
        if args.command == "diff":
            return _diff(args)
        if args.command == "apply-delta":
            return _apply_delta(args)
        if args.command == "apply-schema":
            return _apply_schema(args)
    except TransferError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("database operation failed", file=sys.stderr)
        return 1
    return 2


def _source_url():
    url = os.environ.get("SOURCE_DATABASE_URL", "")
    if not url:
        raise TransferError("SOURCE_DATABASE_URL is not set")
    return url


def _target_url():
    url = os.environ.get("TARGET_DATABASE_URL", "")
    if not url:
        raise TransferError("TARGET_DATABASE_URL is not set")
    return url


def _require_identity(url, expected, label):
    actual = identity(url)
    if expected != actual:
        raise TransferError(f"{label} confirmation does not match")
    return actual


def _require_different(source, target):
    if identity(source) == identity(target):
        raise TransferError("source and target are the same database")


def _window_ok(args) -> bool:
    return bool(args.commit and args.write_window_acknowledged == "writes-frozen")


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, snapshot):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(snapshot), encoding="utf-8")
    os.chmod(destination, 0o600)


def _backup(args):
    if not args.out or not args.confirm_source:
        raise TransferError("backup requires --out and --confirm-source")
    url = _source_url()
    print("source", _require_identity(url, args.confirm_source, "source"))
    conn = connect(url)
    try:
        snapshot = backup(conn, ignore_extra_tables=args.ignore_extra_tables)
    finally:
        conn.close()
    _write(args.out, snapshot)
    counts = {name: len(body["rows"]) for name, body in snapshot["tables"].items()}
    print("backup_tables", json.dumps(counts, sort_keys=True))
    if snapshot["ignored_extra_tables"]:
        print("ignored_extra_tables", ",".join(snapshot["ignored_extra_tables"]))
    return 0


def _restore(args):
    if not args.file or not args.confirm_target:
        raise TransferError("restore requires --file and --confirm-target")
    url = _target_url()
    print("target", _require_identity(url, args.confirm_target, "target"))
    snapshot = _load(args.file)
    conn = connect(url)
    try:
        if not _window_ok(args):
            result = restore(conn, snapshot, commit=False)
            print("dry_restore", json.dumps(result["inserted"], sort_keys=True))
            print("committed false")
            return 0
        result = restore(conn, snapshot, commit=True)
    finally:
        conn.close()
    print("restored", json.dumps(result["inserted"], sort_keys=True))
    print("committed true")
    return 0


def _validate(args):
    if not args.file or not args.confirm_target:
        raise TransferError("validate requires --file and --confirm-target")
    url = _target_url()
    print("target", _require_identity(url, args.confirm_target, "target"))
    snapshot = _load(args.file)
    conn = connect(url)
    try:
        conn.set_session(readonly=True, autocommit=False)
        problems = validate(conn, snapshot)
    finally:
        conn.close()
    if problems:
        print("validation_failed", "; ".join(problems))
        return 1
    print("validation_ok")
    return 0


def _dry_run(args):
    if not args.confirm_source or not args.confirm_target:
        raise TransferError("dry-run requires --confirm-source and --confirm-target")
    source = _source_url()
    target = _target_url()
    _require_different(source, target)
    print("source", _require_identity(source, args.confirm_source, "source"))
    print("target", _require_identity(target, args.confirm_target, "target"))
    src = connect(source)
    try:
        snapshot = backup(src, ignore_extra_tables=args.ignore_extra_tables)
    finally:
        src.close()
    dst = connect(target)
    try:
        result = restore(dst, snapshot, commit=False)
    finally:
        dst.close()
    print("dry_run_rows", json.dumps(result["inserted"], sort_keys=True))
    print("committed false")
    return 0


def _diff(args):
    if not args.file or not args.confirm_target:
        raise TransferError("diff requires --file and --confirm-target")
    url = _target_url()
    print("target", _require_identity(url, args.confirm_target, "target"))
    snapshot = _load(args.file)
    conn = connect(url)
    try:
        report = diff(conn, snapshot)
    finally:
        conn.close()
    printable = {
        table: {key: value for key, value in body.items()}
        for table, body in report.items()
    }
    print("diff", json.dumps(printable, sort_keys=True))
    return 0


def _apply_delta(args):
    if not args.file or not args.confirm_source or not args.confirm_onto:
        raise TransferError("apply-delta requires --file, --confirm-source, and --confirm-onto")
    source = _source_url()
    onto = _target_url()
    _require_different(source, onto)
    print("from", _require_identity(source, args.confirm_source, "source"))
    print("onto", _require_identity(onto, args.confirm_onto, "target"))
    snapshot = _load(args.file)
    src = connect(source)
    dst = connect(onto)
    try:
        result = apply_delta(
            src, dst, snapshot,
            commit=_window_ok(args),
            propagate_deletes=args.propagate_deletes,
        )
    finally:
        src.close()
        dst.close()
    print("delta", json.dumps(result["applied"], sort_keys=True))
    print("committed", "true" if result["committed"] else "false")
    return 0


def _apply_schema(args):
    if not args.confirm_target:
        raise TransferError("apply-schema requires --confirm-target")
    url = _target_url()
    print("target", _require_identity(url, args.confirm_target, "target"))
    if not _window_ok(args):
        raise TransferError("schema was not applied; pass --commit and --write-window-acknowledged writes-frozen")
    conn = connect(url)
    try:
        applied = apply_migrations(conn)
    finally:
        conn.close()
    print("applied", ",".join(applied))
    return 0


if __name__ == "__main__":
    sys.exit(main())
