#!/usr/bin/env python3
"""Portable, standard-library-only SQLite volume storage."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from contextlib import closing

LEGACY_DATABASES = ("article_summaries.db", "article_summaries_kr.db")
ENGLISH_DATABASE = "article_summaries_en.db"
DATABASES = (*LEGACY_DATABASES, ENGLISH_DATABASE)
CHUNK_SIZE = 48 * 1024 * 1024
SIDECARS = ("-wal", "-shm", "-journal")
BLOCK_SIZE = 1024 * 1024


class VolumeError(ValueError):
    pass


def default_databases(data_dir: Path, *, command: str = "restore") -> tuple[str, ...]:
    """Select present languages; absent optional English data is never required.

    Legacy checkouts still require both legacy databases. English-only checkouts
    can use the standalone tool without acquiring either legacy database.
    """
    present = tuple(
        name for name in DATABASES
        if volume_path(data_dir, name).exists() or volume_path(data_dir, name).is_symlink()
        or (command == "pack" and (data_dir / name).is_file())
    )
    legacy_present = any(name in present for name in LEGACY_DATABASES)
    if legacy_present or not present:
        return (*LEGACY_DATABASES, ENGLISH_DATABASE) if ENGLISH_DATABASE in present else LEGACY_DATABASES
    return present


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(BLOCK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def check_sqlite(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise VolumeError(f"Missing or symlinked database: {path}")
    with path.open("rb") as source:
        if source.read(16) != b"SQLite format 3\x00":
            raise VolumeError(f"Invalid SQLite header: {path}")
    # immutable is appropriate only for the closed, standalone snapshots used here.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        results = conn.execute("PRAGMA quick_check").fetchall()
        if results != [("ok",)]:
            raise VolumeError(f"SQLite quick_check failed for {path}: {results}")


def volume_path(data_dir: Path, database: str) -> Path:
    if database not in DATABASES:
        raise VolumeError(f"Unsupported database: {database}")
    return data_dir / f"{database}.parts"


def load_manifest(data_dir: Path, database: str) -> dict:
    directory = volume_path(data_dir, database)
    manifest_path = directory / "manifest.json"
    if directory.is_symlink() or manifest_path.is_symlink():
        raise VolumeError(f"Symlinked volume directory/manifest: {directory}")
    if manifest_path.stat().st_size > 1024 * 1024:
        raise VolumeError(f"Manifest is too large: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise VolumeError(f"Invalid manifest: {manifest_path}")
    if type(manifest.get("format_version")) is not int or manifest["format_version"] != 1:
        raise VolumeError(f"Unsupported manifest format: {manifest_path}")
    if manifest.get("database") != database:
        raise VolumeError(f"Manifest database does not match {database}")
    for key in ("size_bytes", "chunk_size_bytes"):
        if type(manifest.get(key)) is not int or manifest[key] <= 0:
            raise VolumeError(f"Invalid {key}: {manifest_path}")
    if manifest["chunk_size_bytes"] > CHUNK_SIZE:
        raise VolumeError(f"Volume size exceeds 48 MiB: {manifest_path}")

    def valid_digest(value: object) -> bool:
        return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)

    if not valid_digest(manifest.get("sha256")):
        raise VolumeError(f"Invalid database SHA-256: {manifest_path}")
    parts = manifest.get("parts")
    if not isinstance(parts, list) or not parts:
        raise VolumeError(f"Empty or invalid parts list: {manifest_path}")
    expected_count = (manifest["size_bytes"] + manifest["chunk_size_bytes"] - 1) // manifest["chunk_size_bytes"]
    if len(parts) != expected_count:
        raise VolumeError(f"Incorrect part count: {manifest_path}")
    for index, part in enumerate(parts):
        expected_size = min(manifest["chunk_size_bytes"], manifest["size_bytes"] - index * manifest["chunk_size_bytes"])
        if (not isinstance(part, dict) or part.get("name") != f"part-{index:05d}"
                or type(part.get("size_bytes")) is not int or part["size_bytes"] != expected_size
                or not valid_digest(part.get("sha256"))):
            raise VolumeError(f"Invalid part {index}: {manifest_path}")
    expected_files = {"manifest.json", *(part["name"] for part in parts)}
    if {entry.name for entry in directory.iterdir()} != expected_files:
        raise VolumeError(f"Missing or unexpected files in {directory}")
    return manifest


def _assemble(data_dir: Path, database: str, target: Path) -> dict:
    manifest = load_manifest(data_dir, database)
    digest = hashlib.sha256()
    with target.open("wb") as output:
        for part in manifest["parts"]:
            path = volume_path(data_dir, database) / part["name"]
            if path.is_symlink() or not path.is_file() or path.stat().st_size != part["size_bytes"]:
                raise VolumeError(f"Missing, truncated or symlinked part: {path}")
            part_digest = hashlib.sha256()
            with path.open("rb") as source:
                for block in iter(lambda: source.read(BLOCK_SIZE), b""):
                    output.write(block)
                    part_digest.update(block)
                    digest.update(block)
            if part_digest.hexdigest() != part["sha256"]:
                raise VolumeError(f"Part SHA-256 mismatch: {path}")
        output.flush()
        os.fsync(output.fileno())
    if target.stat().st_size != manifest["size_bytes"] or digest.hexdigest() != manifest["sha256"]:
        raise VolumeError(f"Database size/SHA-256 mismatch: {database}")
    check_sqlite(target)
    return manifest


def verify_database(data_dir: Path, database: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="db-volumes-verify-") as temporary:
        return _assemble(data_dir, database, Path(temporary) / database)


def replace_directory(prepared: Path, destination: Path) -> None:
    """Keep the previous generation available until a complete replacement is ready."""
    if destination.is_symlink():
        raise VolumeError(f"Refusing to replace symlink: {destination}")
    if not destination.exists():
        prepared.rename(destination)
        return
    with tempfile.TemporaryDirectory(prefix=".db-volumes-old-", dir=destination.parent) as temporary:
        previous = Path(temporary) / destination.name
        destination.rename(previous)
        try:
            prepared.rename(destination)
        except BaseException:
            previous.rename(destination)
            raise


def pack_database(data_dir: Path, database: str, *, chunk_size: int = CHUNK_SIZE) -> dict:
    if type(chunk_size) is not int or not 1 <= chunk_size <= CHUNK_SIZE:
        raise VolumeError("chunk_size must be between 1 byte and 48 MiB")
    destination = volume_path(data_dir, database)
    source_path = data_dir / database
    if source_path.is_symlink() or not source_path.is_file():
        raise VolumeError(f"Missing or symlinked source database: {source_path}")
    with source_path.open("rb") as source_header:
        if source_header.read(16) != b"SQLite format 3\x00":
            raise VolumeError(f"Invalid source SQLite header: {source_path}")
    with tempfile.TemporaryDirectory(prefix=".db-volumes-pack-", dir=data_dir) as temporary:
        snapshot = Path(temporary) / database
        # mode=rw never creates a missing database. Let SQLite manage/checkpoint
        # its own sidecars on close; a mode=ro WAL connection can leave newly
        # created empty sidecars behind. Never use immutable for this live source.
        with closing(sqlite3.connect(source_path.resolve().as_uri() + "?mode=rw", uri=True)) as source:
            with closing(sqlite3.connect(snapshot)) as target:
                source.backup(target)
        check_sqlite(snapshot)
        prepared = Path(temporary) / destination.name
        prepared.mkdir()
        manifest = {
            "format_version": 1,
            "database": database,
            "size_bytes": snapshot.stat().st_size,
            "sha256": sha256(snapshot),
            "chunk_size_bytes": chunk_size,
            "parts": [],
        }
        with snapshot.open("rb") as source:
            index = 0
            while block := source.read(chunk_size):
                name = f"part-{index:05d}"
                (prepared / name).write_bytes(block)
                manifest["parts"].append({
                    "name": name, "size_bytes": len(block),
                    "sha256": hashlib.sha256(block).hexdigest(),
                })
                index += 1
        (prepared / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        # Verify the actual parts before installing the new generation.
        verify_database(Path(temporary), database)
        replace_directory(prepared, destination)
    return manifest


def restore_database(data_dir: Path, database: str, *, force: bool = False, output_dir: Path | None = None) -> dict:
    destination_dir = output_dir if output_dir is not None else data_dir
    destination_dir.mkdir(parents=True, exist_ok=True)
    target = destination_dir / database
    volume_path(data_dir, database)  # Validate the database name before using it as a path.
    if target.is_symlink():
        raise VolumeError(f"Refusing to replace symlink: {target}")

    def check_sidecars() -> None:
        for suffix in SIDECARS:
            sidecar = Path(str(target) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                raise VolumeError(f"Close SQLite connections and resolve sidecar before restoring: {sidecar}")

    check_sidecars()
    with tempfile.TemporaryDirectory(prefix=".db-volumes-restore-", dir=destination_dir) as temporary:
        prepared = Path(temporary) / database
        manifest = _assemble(data_dir, database, prepared)
        if target.exists():
            if sha256(target) == manifest["sha256"]:
                return manifest
            if not force:
                raise VolumeError(f"Existing database differs: {target}; pack local edits first, or use --force after closing all connections")
        check_sidecars()
        os.replace(prepared, target)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("pack", "restore", "verify"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--data-dir", type=Path, default=Path("data"))
        subparser.add_argument("--database", choices=DATABASES, action="append", help="Repeat to select databases; default: present languages (legacy pair required together)")
        if command == "restore":
            subparser.add_argument("--force", action="store_true", help="Replace differing files; close all SQLite connections first")
            subparser.add_argument("--output-dir", type=Path, help="Restore into a separate directory, e.g. a release staging directory")
    args = parser.parse_args(argv)
    try:
        for database in dict.fromkeys(args.database or default_databases(args.data_dir, command=args.command)):
            if args.command == "pack":
                manifest = pack_database(args.data_dir, database)
            elif args.command == "restore":
                manifest = restore_database(args.data_dir, database, force=args.force, output_dir=args.output_dir)
            else:
                manifest = verify_database(args.data_dir, database)
            print(f"{args.command}: {database}: {manifest['size_bytes']} bytes, {len(manifest['parts'])} part(s)")
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
