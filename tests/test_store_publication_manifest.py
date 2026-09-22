from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_store_publication_manifest",
    ROOT / "scripts" / "build_store_publication_manifest.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True)
    store = tmp_path / "data" / "market-data"
    seed = store / "collector-state" / "seed.json"
    seed.parent.mkdir(parents=True, exist_ok=True)
    seed.write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "data/market-data"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "seed"], check=True)
    return tmp_path, store


def test_manifest_binds_actual_changed_and_declared_output_blobs(tmp_path: Path) -> None:
    repo, store = _repo(tmp_path)
    snapshot = store / "snapshots" / "2026-09" / "fixture.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text('{"ok":true}\n', encoding="utf-8")
    result_file = tmp_path / "result.txt"
    result_file.write_text(
        json.dumps({"snapshot_path": "snapshots/2026-09/fixture.json"}) + "\n",
        encoding="utf-8",
    )

    manifest = MODULE.build_manifest(
        store_root=store,
        result_file=result_file,
        request_id="req-1",
        trade_date="2026-09-22",
        stage="open_30m",
    )
    assert manifest is not None
    value = json.loads(manifest.read_text(encoding="utf-8"))
    outputs = {row["path"]: row["blob_sha"] for row in value["required_outputs"]}
    raw = snapshot.read_bytes()
    assert outputs["snapshots/2026-09/fixture.json"] == MODULE._git_blob_sha(raw)


def test_manifest_fails_when_producer_declares_missing_output(tmp_path: Path) -> None:
    _repo(tmp_path)
    result_file = tmp_path / "result.txt"
    result_file.write_text(
        json.dumps({"snapshot_path": "snapshots/2026-09/missing.json"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(MODULE.PublicationManifestError, match="declared output does not exist"):
        MODULE.build_manifest(
            store_root=tmp_path / "data" / "market-data",
            result_file=result_file,
            request_id="req-1",
            trade_date="2026-09-22",
            stage="open_30m",
        )


def test_manifest_returns_none_when_run_has_no_store_output(tmp_path: Path) -> None:
    _repo(tmp_path)
    result_file = tmp_path / "result.txt"
    result_file.write_text(json.dumps({"status": "no_new_qualified_facts"}) + "\n", encoding="utf-8")
    assert MODULE.build_manifest(
        store_root=tmp_path / "data" / "market-data",
        result_file=result_file,
        request_id="req-1",
        trade_date="2026-09-22",
        stage="open_30m",
    ) is None
