#!/usr/bin/env python3
"""Backup/restore handover drill for Reel Loom (声画织场).

Everything runs under /tmp/loom-drill with throwaway data directories.
The real service is exercised over HTTP; this script only drives it,
takes filesystem-level backups, kills/restarts processes and records evidence.
"""
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path("/tmp/loom-drill")
EVID = ROOT / "evidence"
SLOW_FFMPEG = ROOT / "scripts" / "slow-ffmpeg"
REAL_FFMPEG = Path("/tmp/loom-deps/imageio_ffmpeg/binaries/ffmpeg-linux-aarch64-v7.0.2")
PORT = 8841
BASE = f"http://127.0.0.1:{PORT}/loom/v1"
PYTHON = sys.executable
ENV = {
    **os.environ,
    "PYTHONPATH": "/workspace:/tmp/loom-deps:/tmp/loom-pylibs/lib/python3.11/site-packages",
    "LOOM_HOST": "127.0.0.1",
    "LOOM_PORT": str(PORT),
    "LOOM_WORKERS": "1",
}
log_lines = []
LOG_PATH = EVID / "drill-timeline.log"


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    log_lines.append(line)
    with LOG_PATH.open("a") as fh:
        fh.write(line + "\n")


def http(method, path, body=None, headers=None, raw=False):
    data = None
    h = dict(headers or {})
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)
            h.setdefault("Content-Type", "application/octet-stream")
        else:
            data = json.dumps(body, ensure_ascii=False).encode()
            h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read()
            return payload if raw else json.loads(payload)
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read())


def wait_health(proc, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early: {proc.returncode}")
        try:
            return http("GET", "/health")
        except Exception:
            time.sleep(0.2)
    raise TimeoutError("server did not become healthy")


def start_server(data_dir, ffmpeg):
    env = {**ENV, "LOOM_DATA": str(data_dir), "LOOM_FFMPEG": str(ffmpeg)}
    log_path = open(EVID / f"server-{data_dir.name}.log", "a")
    proc = subprocess.Popen([PYTHON, "-m", "reel_loom.server"], env=env,
                            cwd="/workspace", stdout=log_path, stderr=subprocess.STDOUT,
                            start_new_session=True)
    health = wait_health(proc)
    log(f"server started pid={proc.pid} data={data_dir.name} health={json.dumps(health, sort_keys=True)}")
    return proc, log_path


def stop(proc, log_path):
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    if not log_path.closed:
        log_path.close()
    log(f"server exited rc={proc.returncode}")


def graceful_stop(proc, log_path):
    """The app only catches KeyboardInterrupt (SIGINT); give it time to finish."""
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            log("SIGINT did not end the process in time; escalating to SIGTERM")
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
    if not log_path.closed:
        log_path.close()
    log(f"server exited rc={proc.returncode}")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot_dir_layout(data_dir, label):
    """Record database tables, blob files, scratch contents and hashes."""
    db_file = data_dir / "reel_loom.sqlite3"
    info = {"label": label, "path": str(data_dir), "exists": db_file.exists(),
            "wal": db_file.with_suffix(".sqlite3-wal").exists(),
            "blobs": [], "scratch": [], "top": sorted(p.name for p in data_dir.iterdir())}
    if db_file.exists():
        uri = f"file:{db_file}?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        for table in ("blobs", "assets", "reels", "revisions", "looks", "tickets",
                      "artifacts", "imprints", "cue_tracks", "events"):
            try:
                info[f"count_{table}"] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error as exc:
                info[f"count_{table}"] = f"ERROR {exc}"
        info["ticket_rows"] = [dict(zip(("id", "state", "cancel", "attempt", "error"), r))
                               for r in con.execute(
                                   "SELECT id,state,cancel,attempt,error FROM tickets ORDER BY created,id")]
        con.close()
    blobs_dir = data_dir / "blobs"
    if blobs_dir.exists():
        for path in sorted(blobs_dir.rglob("*")):
            if path.is_file():
                info["blobs"].append({"rel": str(path.relative_to(blobs_dir)),
                                      "size": path.stat().st_size, "sha256": sha256_file(path)})
    scratch = data_dir / "scratch"
    if scratch.exists():
        for path in sorted(scratch.rglob("*")):
            info["scratch"].append({"rel": str(path.relative_to(scratch)),
                                    "is_dir": path.is_dir(),
                                    "size": path.stat().st_size if path.is_file() else None})
    (EVID / f"layout-{label}.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
    log(f"layout[{label}]: blobs={len(info['blobs'])} scratch_entries={len(info['scratch'])} "
        f"tickets={info.get('ticket_rows')}")
    return info


def make_sample(destination):
    """Generate the same bounded bars+tone sample the demo uses."""
    subprocess.run([str(REAL_FFMPEG), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=3",
                    "-f", "lavfi", "-i", "sine=frequency=659:sample_rate=48000:duration=3",
                    "-af", "volume=enable='between(t,1,1.5)':volume=0",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    str(destination)], check=True)


def main():
    for old in EVID.glob("*"):
        old.unlink()
    primary = ROOT / "primary"
    restored = ROOT / "restored"
    restored_partial = ROOT / "restored-partial"
    backup_dir = ROOT / "backup"
    for d in (primary, restored, restored_partial, backup_dir):
        if d.exists():
            shutil.rmtree(d)
    results = {}

    # ---- phase 1: build real history in the primary workspace --------------
    proc, logf = start_server(primary, SLOW_FFMPEG)
    try:
        sample = ROOT / "sample.mp4"
        make_sample(sample)
        asset = http("POST", "/assets", sample.read_bytes(), {"X-Media-Label": "drill-source"})
        assert asset["state"] == "ready", asset
        slices = [{"asset": asset["id"], "in": 0.2, "out": 1.2, "label": "a"},
                  {"asset": asset["id"], "in": 1.6, "out": 2.8, "label": "b"}]
        reel = http("POST", "/reels", {"title": "演练卷", "slices": slices})
        look = http("PUT", "/looks/preview", {"kind": "video", "width": 320, "height": 180},
                    {"If-Match": "0"})
        audio_look = http("PUT", "/looks/listening", {"kind": "audio", "sample_rate": 48000},
                          {"If-Match": "0"})
        results["asset_id"] = asset["id"]
        results["asset_blob"] = asset["blob"]
        results["reel_id"] = reel["id"]
        log(f"ingested asset={asset['id']} blob={asset['blob'][:12]}... reel={reel['id']}")

        t1 = http("POST", "/tickets", {"reel": reel["id"], "look": "preview"})
        t2 = http("POST", "/tickets", {"reel": reel["id"], "look": "listening"})
        results["ticket_video"] = t1["id"]
        results["ticket_audio"] = t2["id"]
        log(f"queued tickets {t1['id']} (video), {t2['id']} (audio)")

        # ---- phase 2: take a cold-style whole-directory backup WHILE running
        deadline = time.monotonic() + 20
        while True:
            running = http("GET", f"/tickets/{t1['id']}")
            if running["state"] == "running":
                break
            if time.monotonic() > deadline:
                raise RuntimeError("ticket never reached running state")
            time.sleep(0.1)
        log(f"ticket {t1['id']} observed running, taking whole-directory backup now")
        before = snapshot_dir_layout(primary, "primary-during-running")
        results["running_progress_at_backup"] = running["progress"]
        shutil.copytree(primary, backup_dir)
        backup_layout = snapshot_dir_layout(backup_dir, "backup-taken-while-running")
        results["backup_blob_count"] = len(backup_layout["blobs"])

        # ---- phase 3: abnormal interruption (SIGKILL, like power loss) -----
        os.kill(proc.pid, signal.SIGKILL)
        stop(proc, logf)
        log("primary killed with SIGKILL while a render was running")
        after_kill = snapshot_dir_layout(primary, "primary-after-sigkill")
        results["scratch_after_kill"] = after_kill["scratch"]
        results["ticket_rows_after_kill"] = after_kill["ticket_rows"]
    finally:
        if proc.poll() is None:
            os.kill(proc.pid, signal.SIGKILL)
            stop(proc, logf)

    # ---- phase 4: restore the full backup into a fresh directory ------------
    shutil.copytree(backup_dir, restored)
    snapshot_dir_layout(restored, "restored-before-start")
    proc2, logf2 = start_server(restored, SLOW_FFMPEG)
    try:
        health = http("GET", "/health")
        results["recovery_report"] = health["recovery"]
        log(f"restored service health recovery={health['recovery']}")
        tv = http("GET", f"/tickets/{results['ticket_video']}")
        ta = http("GET", f"/tickets/{results['ticket_audio']}")
        results["video_ticket_after_recovery"] = {"state": tv["state"], "error": tv["error"],
                                                  "attempt": tv["attempt"], "artifacts": tv["artifacts"]}
        results["audio_ticket_after_recovery"] = {"state": ta["state"], "attempt": ta["attempt"]}
        log(f"recovered states: video={tv['state']}({tv['error']}) audio={ta['state']}")

        # ---- phase 5: explicit retry reuses the frozen snapshot -------------
        retry = http("POST", f"/tickets/{results['ticket_video']}/retry")
        assert retry["state"] == "queued", retry
        deadline = time.monotonic() + 40
        while True:
            tv2 = http("GET", f"/tickets/{results['ticket_video']}")
            if tv2["state"] in ("succeeded", "failed", "cancelled"):
                break
            if time.monotonic() > deadline:
                raise RuntimeError("retried render did not finish")
            time.sleep(0.3)
        results["retry_outcome"] = {"state": tv2["state"], "attempt": tv2["attempt"],
                                    "artifact_count": len(tv2["artifacts"]),
                                    "fingerprint_unchanged": tv2["fingerprint"] == t1["fingerprint"]}
        log(f"retry outcome: {results['retry_outcome']}")
        artifact = tv2["artifacts"][0]
        results["artifact_id"] = artifact["id"]
        results["artifact_blob"] = artifact["blob"]

        # user-visible proof of recovery: conformance (full decode) + download
        conf = http("GET", f"/artifacts/{artifact['id']}/conformance")
        results["conformance_valid"] = conf["valid"]
        results["conformance_checks"] = [{"check": c["check"], "passed": c["passed"]}
                                         for c in conf["checks"]]
        content = http("GET", f"/artifacts/{artifact['id']}/content", raw=True)
        results["download_bytes"] = len(content)
        results["download_sha256_matches"] = hashlib.sha256(content).hexdigest() == artifact["blob"]
        log(f"conformance valid={conf['valid']}; download {len(content)} bytes "
            f"sha256_match={results['download_sha256_matches']}")

        # imprint the finished render and verify the package
        imprint = http("POST", f"/tickets/{results['ticket_video']}/imprints", {"cue_tracks": []})
        verified = http("GET", f"/imprints/{imprint['id']}/verify")
        results["imprint_id"] = imprint["id"]
        results["imprint_verify"] = verified["valid"]
        results["imprint_blob"] = imprint["blob"]
        log(f"imprint {imprint['id']} verify={verified['valid']}")

        # lineage closure proof
        lineage = http("GET", f"/tickets/{results['ticket_video']}/lineage")
        kinds = sorted({n["kind"] for n in lineage["nodes"]})
        results["lineage_node_kinds"] = kinds
        results["lineage_edge_count"] = len(lineage["edges"])
        log(f"lineage node kinds={kinds} edges={len(lineage['edges'])}")

        # reconcile: the restored workspace must be internally consistent
        reconcile = http("GET", "/resources")
        results["reconcile_after_recovery"] = reconcile
        log(f"reconcile consistent={reconcile['consistent']} "
            f"missing={len(reconcile['missing'])} bad_size={len(reconcile['bad_size'])} "
            f"unregistered={len(reconcile['unregistered_blobs'])}")
        snapshot_dir_layout(restored, "restored-after-delivery")
    finally:
        stop(proc2, logf2)

    # ---- phase 6: anti-pattern A — SQLite file copied without blobs --------
    shutil.copytree(backup_dir, restored_partial)
    blob_root = restored_partial / "blobs"
    removed = 0
    for path in blob_root.rglob("*"):
        if path.is_file():
            path.unlink()
            removed += 1
    # also remove scratch to mirror a "clean copy"
    shutil.rmtree(restored_partial / "scratch")
    (restored_partial / "scratch").mkdir()
    results["partial_restore_blob_files_removed"] = removed
    proc3, logf3 = start_server(restored_partial, REAL_FFMPEG)
    try:
        health3 = http("GET", "/health")
        results["partial_health_database"] = health3["database"]
        results["partial_health_blobs_stat"] = health3["blobs"]
        log(f"partial copy: /health database={health3['database']} blobs={health3['blobs']} "
            f"(DB opens normally despite missing bytes)")
        tv3 = http("GET", f"/tickets/{results['ticket_video']}")
        results["partial_ticket_looks_normal"] = {"state": tv3["state"],
                                                  "snapshot_sources": len(tv3["snapshot"]["sources"])}
        # the failure must surface when actual bytes are demanded
        retry3 = http("POST", f"/tickets/{results['ticket_video']}/retry")
        deadline = time.monotonic() + 30
        failure = None
        while time.monotonic() < deadline:
            row = http("GET", f"/tickets/{results['ticket_video']}")
            if row["state"] in ("failed", "cancelled"):
                failure = row["error"]
                break
            time.sleep(0.3)
        results["partial_retry_error"] = failure
        log(f"partial copy retry error: {failure}")
        reconcile3 = http("GET", "/resources")
        results["partial_reconcile"] = {"consistent": reconcile3["consistent"],
                                        "missing_count": len(reconcile3["missing"]),
                                        "missing_sample": reconcile3["missing"][:2]}
        log(f"partial reconcile consistent={reconcile3['consistent']} "
            f"missing={len(reconcile3['missing'])}")
    finally:
        stop(proc3, logf3)

    # ---- phase 7: normal shutdown vs abnormal interruption ------------------
    graceful_dir = ROOT / "graceful"
    if graceful_dir.exists():
        shutil.rmtree(graceful_dir)
    proc4, logf4 = start_server(graceful_dir, SLOW_FFMPEG)
    try:
        asset4 = http("POST", "/assets", sample.read_bytes(), {"X-Media-Label": "graceful-source"})
        reel4 = http("POST", "/reels", {"title": "关停演练",
                                        "slices": [{"asset": asset4["id"], "in": 0.1, "out": 2.0}]})
        http("PUT", "/looks/preview", {"kind": "video", "width": 320, "height": 180},
             {"If-Match": "0"})
        t4 = http("POST", "/tickets", {"reel": reel4["id"], "look": "preview"})
        deadline = time.monotonic() + 20
        while http("GET", f"/tickets/{t4['id']}")["state"] != "running":
            time.sleep(0.1)
        log(f"graceful ticket {t4['id']} running; sending SIGINT (Ctrl-C, the only graceful path)")
        graceful_stop(proc4, logf4)
        results["graceful_exit_code"] = proc4.returncode
        # restart and observe the persisted terminal state
        proc5, logf5 = start_server(graceful_dir, SLOW_FFMPEG)
        try:
            row = http("GET", f"/tickets/{t4['id']}")
            results["graceful_ticket_after_restart"] = {"state": row["state"], "error": row["error"]}
            results["graceful_recovery_report"] = http("GET", "/health")["recovery"]
            log(f"graceful shutdown ticket state after restart: {row['state']} {row['error']}")
        finally:
            stop(proc5, logf5)
    finally:
        if proc4.poll() is None:
            stop(proc4, logf4)

    (EVID / "drill-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    (EVID / "drill-timeline.log").write_text("\n".join(log_lines) + "\n")
    log("drill complete; evidence written to " + str(EVID))


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        import traceback
        with LOG_PATH.open("a") as fh:
            fh.write("DRILL FAILED: " + repr(exc) + "\n")
            fh.write(traceback.format_exc())
        raise
