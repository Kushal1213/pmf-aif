"""Single-container entrypoint for free hosting (Koyeb free instance, Hugging Face, any Docker host).

With a SQLite DATABASE_URL:
1. If the database is missing and SNAPSHOT_REPO is set, restore the latest snapshot from that Hugging Face
   dataset (HF_TOKEN needed for a private dataset).
2. Serve the API + UI with uvicorn on $PORT.
3. Optionally keep data current in-process (REFRESH_HOURS > 0: `app.ingest refresh --with-metrics` in a child
   process, then upload a snapshot when HF_TOKEN can write). On tiny instances leave this off and let the
   scheduled GitHub Actions job refresh the snapshot instead.
4. Optionally watch the dataset (SNAPSHOT_CHECK_HOURS > 0): when a newer snapshot is published, exit so the
   platform restarts the container onto it.
With any other database URL (e.g. PostgreSQL) it just serves.

Environment: DATABASE_URL, PORT (8000), SNAPSHOT_REPO, HF_TOKEN, REFRESH_HOURS (0), REFRESH_RPS (5),
SNAPSHOT_CHECK_HOURS (0), TRUST_FORWARDED_FOR.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn

from . import snapshot as snap

log = logging.getLogger("serve")


def _run(*args: str) -> int:
    log.info("job start: %s", " ".join(args))
    rc = subprocess.call([sys.executable, "-m", "app.ingest", *args])
    log.info("job end (%d): %s", rc, " ".join(args))
    return rc


def _has_data(db: Path) -> bool:
    if not db.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return (con.execute("select count(*) from navs").fetchone() or [0])[0] > 0
        finally:
            con.close()
    except sqlite3.Error:
        return False


def _remote_revision(repo: str) -> str | None:
    try:
        from huggingface_hub import HfApi
        return HfApi(token=os.getenv("HF_TOKEN")).dataset_info(repo).sha
    except Exception as e:  # noqa: BLE001 - a failed check just means "no change known"
        log.warning("snapshot revision check failed: %s", e)
        return None


def keep_fresh(db: Path, repo: str | None, hours: float, rps: str) -> None:
    while True:
        try:
            _run("load" if not _has_data(db) else "refresh", "--rps", rps, "--with-metrics")
            if repo and os.getenv("HF_TOKEN"):
                _run("snapshot", "--db", str(db), "--out", str(db.parent / "snapshot" / snap.SNAPSHOT_FILE), "--repo", repo)
        except Exception:  # noqa: BLE001 - the scheduler must survive any job failure
            log.exception("background job failed")
        time.sleep(max(0.25, hours) * 3600)


def watch_snapshot(repo: str, loaded: str | None, hours: float) -> None:
    while True:
        time.sleep(max(0.25, hours) * 3600)
        rev = _remote_revision(repo)
        if rev and loaded and rev != loaded:
            log.info("newer snapshot %s published (running %s); restarting to load it", rev[:8], loaded[:8])
            os._exit(3)          # non-zero so the platform restarts the container


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    url = os.environ.setdefault("DATABASE_URL", "sqlite:///data/mf.db")
    port = int(os.getenv("PORT", "8000"))
    if url.startswith("sqlite:///"):
        db = snap.sqlite_path(url)
        db.parent.mkdir(parents=True, exist_ok=True)
        repo = os.getenv("SNAPSHOT_REPO") or None
        loaded = None
        if repo:
            loaded = _remote_revision(repo)
            if not _has_data(db):
                try:
                    snap.download(repo, db)
                except Exception:  # noqa: BLE001 - serve (empty) rather than crash-loop
                    log.exception("snapshot restore failed; starting without it")
        hours = float(os.getenv("REFRESH_HOURS", "0"))
        if hours > 0:
            threading.Thread(target=keep_fresh, args=(db, repo, hours, os.getenv("REFRESH_RPS", "5")),
                             name="keep-fresh", daemon=True).start()
        check = float(os.getenv("SNAPSHOT_CHECK_HOURS", "0"))
        if repo and check > 0 and hours <= 0:
            threading.Thread(target=watch_snapshot, args=(repo, loaded, check), name="watch", daemon=True).start()
    behind_proxy = os.getenv("TRUST_FORWARDED_FOR", "").lower() in ("1", "true", "yes")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port,
                proxy_headers=behind_proxy, forwarded_allow_ips="*" if behind_proxy else None, log_level="info")


if __name__ == "__main__":
    main()
