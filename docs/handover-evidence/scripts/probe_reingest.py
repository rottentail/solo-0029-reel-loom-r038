#!/usr/bin/env python3
"""Prove the safe remediation path: re-ingest identical originals rewrites a
missing blob file without creating duplicate asset/blob rows."""
import json
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, "/workspace")
sys.path.insert(0, "/tmp/loom-deps")
sys.path.insert(0, "/tmp/loom-pylibs/lib/python3.11/site-packages")

from reel_loom.config import Settings  # noqa: E402
from reel_loom.application import Application  # noqa: E402

FFMPEG = "/tmp/loom-deps/imageio_ffmpeg/binaries/ffmpeg-linux-aarch64-v7.0.2"
EVID = Path("/tmp/loom-drill/evidence")
out = {}
work = Path(tempfile.mkdtemp(prefix="loom-reingest-", dir="/tmp/loom-drill"))
settings = replace(Settings.from_env(), root=work, ffmpeg=FFMPEG, render_timeout=30)
content = Path("/tmp/loom-drill/sample.mp4").read_bytes()

app = Application(settings, start_workers=False)
try:
    asset1 = app.assets.ingest(content, "original")
    blob_path = app.blobs.path(asset1["blob"])
    out["initial"] = {"asset_id": asset1["id"], "blob": asset1["blob"],
                      "blob_rows": app.db.connect().execute("SELECT COUNT(*) FROM blobs").fetchone()[0],
                      "asset_rows": app.db.connect().execute("SELECT COUNT(*) FROM assets").fetchone()[0]}

    # simulate lost media bytes (e.g. partial restore)
    blob_path.unlink()
    out["after_delete"] = app.resources.reconcile()

    # remediation: re-send the identical original bytes
    asset2 = app.assets.ingest(content, "original-again")
    out["after_reingest"] = {"same_asset_id": asset2["id"] == asset1["id"],
                             "same_blob": asset2["blob"] == asset1["blob"],
                             "file_restored": blob_path.is_file(),
                             "blob_rows": app.db.connect().execute("SELECT COUNT(*) FROM blobs").fetchone()[0],
                             "asset_rows": app.db.connect().execute("SELECT COUNT(*) FROM assets").fetchone()[0],
                             "reconcile": app.resources.reconcile(),
                             "verify": app.blobs.resolve(asset1["blob"], verify=True).name}
finally:
    app.close()
shutil.rmtree(work)
(EVID / "reingest-remediation.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
print(json.dumps(out, ensure_ascii=False, indent=2))
