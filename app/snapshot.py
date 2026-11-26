"""Portable database snapshots for free-tier hosting.

Free hosts (Render free tier) have no persistent disk, so the SQLite database is kept as a gzip snapshot
in a private Hugging Face dataset repo: the container restores it on boot, a scheduled job refreshes it
from mfapi.in and uploads a new snapshot. `VACUUM INTO` gives a consistent, compact copy while the API
keeps serving reads from the live file.
"""
from __future__ import annotations

import gzip
import logging
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

log = logging.getLogger("snapshot")
SNAPSHOT_FILE = "mf.db.gz"


def sqlite_path(url: str | None = None) -> Path:
    url = url or os.getenv("DATABASE_URL", "sqlite:///mf.db")
    if not url.startswith("sqlite:///"):
        raise ValueError("snapshots are only supported for SQLite databases")
    return Path(url[len("sqlite:///"):])


def make_snapshot(db: Path, out: Path) -> Path:
    """Write a gzip-compressed, consistent copy of `db` to `out`."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent) as tmp:
        copy = Path(tmp) / "copy.db"
        con = sqlite3.connect(db)
        try:
            con.execute("VACUUM INTO ?", (str(copy),))
        finally:
            con.close()
        part = out.with_suffix(out.suffix + ".part")
        with open(copy, "rb") as src, gzip.open(part, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, length=1 << 20)
        part.replace(out)
    log.info("snapshot %s: %.1f MB", out, out.stat().st_size / 1e6)
    return out


def export_hosted(src: Path, out: Path, years: int = 5, active_days: int = 30, margin_days: int = 15) -> dict:
    """Build the hosted (size-limited) database from a full one.

    Keeps every scheme's metadata and cached metrics, but NAV rows only for schemes with a NAV within
    `active_days` of the newest NAV, and only from `years` years (+ `margin_days`) before that date, which
    is enough for every 1w-5y return, 1y/3y/5y risk, rolling returns and SIP up to 5 years.
    Dates are relative to the newest NAV in the source, so the result is reproducible.
    """
    from datetime import date as _date, timedelta
    from sqlalchemy import create_engine
    from .db import metadata

    if years < 1 or active_days < 1 or margin_days < 0:
        raise ValueError("years and active_days must be >= 1, margin_days >= 0")
    out.parent.mkdir(parents=True, exist_ok=True)
    for f in (out, out.with_name(out.name + "-wal"), out.with_name(out.name + "-shm")):
        f.unlink(missing_ok=True)
    eng = create_engine(f"sqlite:///{out}")
    metadata.create_all(eng)
    eng.dispose()
    con = sqlite3.connect(out)
    try:
        con.execute("ATTACH DATABASE ? AS src", (str(src),))
        ref = con.execute("SELECT max(last_nav_date) FROM src.schemes").fetchone()[0]
        if ref is None:
            raise ValueError("source database has no NAV data")
        ref_d = _date.fromisoformat(ref)
        active_cut = (ref_d - timedelta(days=active_days)).isoformat()
        try:
            hist_cut = ref_d.replace(year=ref_d.year - years)
        except ValueError:                      # 29 Feb
            hist_cut = ref_d.replace(year=ref_d.year - years, day=28)
        hist_cut = (hist_cut - timedelta(days=margin_days)).isoformat()
        with con:
            con.execute("INSERT INTO schemes SELECT * FROM src.schemes")
            con.execute("INSERT INTO scheme_metrics SELECT * FROM src.scheme_metrics")
            con.execute("""INSERT INTO navs SELECT n.* FROM src.navs n
                           JOIN src.schemes s ON s.scheme_code = n.scheme_code
                           WHERE s.last_nav_date >= ? AND n.date >= ?""", (active_cut, hist_cut))
        stats = {"schemes": con.execute("SELECT count(*) FROM schemes").fetchone()[0],
                 "schemes_with_history": con.execute("SELECT count(DISTINCT scheme_code) FROM navs").fetchone()[0],
                 "nav_rows": con.execute("SELECT count(*) FROM navs").fetchone()[0],
                 "reference_date": ref, "history_from": hist_cut}
        con.execute("DETACH DATABASE src")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("VACUUM")
    finally:
        con.close()
    stats["bytes"] = out.stat().st_size
    log.info("hosted export %s: %s", out, stats)
    return stats


def restore_file(gz: Path, db: Path) -> Path:
    """Decompress a snapshot into place atomically (never leaves a half-written database)."""
    db.parent.mkdir(parents=True, exist_ok=True)
    part = db.with_suffix(db.suffix + ".part")
    with gzip.open(gz, "rb") as src, open(part, "wb") as dst:
        shutil.copyfileobj(src, dst, length=1 << 20)
    for extra in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        extra.unlink(missing_ok=True)
    part.replace(db)
    return db


def upload(repo: str, gz: Path, token: str | None = None) -> None:
    from huggingface_hub import HfApi   # optional dependency, only needed where snapshots are published
    api = HfApi(token=token or os.getenv("HF_TOKEN"))
    api.upload_file(path_or_fileobj=str(gz), path_in_repo=SNAPSHOT_FILE, repo_id=repo, repo_type="dataset",
                    commit_message="Update NAV database snapshot")
    try:   # keep only the latest snapshot so daily uploads don't accumulate storage
        api.super_squash_history(repo_id=repo, repo_type="dataset", commit_message="Latest NAV database snapshot")
    except Exception as e:  # noqa: BLE001 - squashing is housekeeping, never fail the publish for it
        log.warning("could not squash dataset history: %s", e)
    log.info("uploaded %s to dataset %s", gz.name, repo)


def download(repo: str, db: Path, token: str | None = None) -> bool:
    """Restore the latest snapshot from the dataset repo; False when there is none yet."""
    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import EntryNotFoundError, RepositoryNotFoundError
    dl = db.parent / "download"
    try:
        gz = Path(hf_hub_download(repo_id=repo, filename=SNAPSHOT_FILE, repo_type="dataset",
                                  token=token or os.getenv("HF_TOKEN"), local_dir=str(dl)))
    except (EntryNotFoundError, RepositoryNotFoundError) as e:
        log.warning("no snapshot in %s: %s", repo, e)
        return False
    try:
        restore_file(gz, db)
    finally:
        shutil.rmtree(dl, ignore_errors=True)     # small disks (2 GB on free instances): drop the .gz at once
    log.info("restored %s from dataset %s", db, repo)
    return True
