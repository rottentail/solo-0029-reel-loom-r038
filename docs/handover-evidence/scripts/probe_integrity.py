#!/usr/bin/env python3
"""Supplementary probes: same-size byte corruption detection and instance lock."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/workspace")
sys.path.insert(0, "/tmp/loom-deps")
sys.path.insert(0, "/tmp/loom-pylibs/lib/python3.11/site-packages")

from dataclasses import replace  # noqa: E402
from reel_loom.config import Settings  # noqa: E402
from reel_loom.application import Application  # noqa: E402
from reel_loom.errors import LoomError  # noqa: E402

FFMPEG = "/tmp/loom-deps/imageio_ffmpeg/binaries/ffmpeg-linux-aarch64-v7.0.2"
EVID = Path("/tmp/loom-drill/evidence")
out = {}

work = Path(tempfile.mkdtemp(prefix="loom-probe-", dir="/tmp/loom-drill"))
settings = Settings.from_env()
settings = replace(settings, root=work, ffmpeg=FFMPEG, render_timeout=30)
content = Path("/tmp/loom-drill/sample.mp4").read_bytes()

app = Application(settings, start_workers=False)
try:
    asset = app.assets.ingest(content, "probe-source")
    blob_path = app.blobs.path(asset["blob"])
    size = blob_path.stat().st_size

    # 1) intact baseline
    out["intact_reconcile"] = app.resources.reconcile()
    out["intact_resolve_verify"] = app.blobs.resolve(asset["blob"], verify=True).name

    # 2) same-size corruption: flip one byte in the middle
    data = bytearray(blob_path.read_bytes())
    data[size // 2] ^= 0xFF
    blob_path.write_bytes(bytes(data))
    assert blob_path.stat().st_size == size, "corruption must preserve size"
    out["same_size_corruption"] = {"size_unchanged": blob_path.stat().st_size == size,
                                   "reconcile": app.resources.reconcile(),
                                   "note": "reconcile checks existence and size only"}
    try:
        app.blobs.resolve(asset["blob"], verify=True)
        out["verify_after_corruption"] = "UNEXPECTED PASS"
    except LoomError as exc:
        out["verify_after_corruption"] = exc.as_dict()
    try:
        app.assets.restore(asset["id"])
        out["restore_after_corruption"] = "UNEXPECTED PASS"
    except LoomError as exc:
        out["restore_after_corruption"] = exc.as_dict()

    # 3) the DB itself still opens and reports healthy
    out["health_after_corruption"] = app.health()
finally:
    app.close()

# 4) instance lock: a second application on the same root must be refused
app2 = Application(settings, start_workers=False)
try:
    try:
        Application(settings, start_workers=False)
        out["instance_lock"] = "UNEXPECTED SECOND INSTANCE"
    except LoomError as exc:
        out["instance_lock"] = exc.as_dict()
finally:
    app2.close()

shutil.rmtree(work)
(EVID / "supplemental-probes.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
print(json.dumps(out, ensure_ascii=False, indent=2))
