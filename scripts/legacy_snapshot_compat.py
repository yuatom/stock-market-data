#!/usr/bin/env python3
"""Create non-authoritative compatibility projections from immutable Snapshots.

The source Snapshot is always read from a pinned Git object. Only diagnostic
reason-class arrays that fail the current uniqueItems contract may change.
This tool never writes Store data or changes an input path.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

TRANSFORM_VERSION = "legacy-reason-classes-canonicalize-v1"
SNAPSHOT_PREFIX = "data/market-data/snapshots/"
HEX40 = re.compile(r"^[0-9a-f]{40}$")


class CompatibilityTransformError(ValueError):
    """Raised when an input cannot be transformed without changing facts."""


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise CompatibilityTransformError(f"document cannot be canonicalized: {exc}") from exc


def git_blob_sha_bytes(raw: bytes) -> str:
    header = f"blob {len(raw)}\0".encode("ascii")
    return hashlib.sha1(header + raw).hexdigest()


def json_pointer(parts: Iterable[Any]) -> str:
    def escape(part: Any) -> str:
        return str(part).replace("~", "~0").replace("/", "~1")
    return "/" + "/".join(escape(p) for p in parts)


def _safe_snapshot_path(value: str) -> str:
    if not isinstance(value, str) or "\\" in value or value.startswith("/"):
        raise CompatibilityTransformError("original path must be a repository-relative POSIX path")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise CompatibilityTransformError("original path contains an unsafe component")
    if not value.startswith(SNAPSHOT_PREFIX) or path.suffix != ".json" or any(part == "latest" or part.startswith("latest.") for part in path.parts):
        raise CompatibilityTransformError("only immutable Snapshot JSON paths may be projected")
    return value


def _error_path(error: Any) -> tuple[Any, ...]:
    return tuple(error.absolute_path)


def _canonical_array(before: Any, pointer: str) -> list[str]:
    if not isinstance(before, list) or not all(isinstance(value, str) for value in before):
        raise CompatibilityTransformError(f"reason_classes value at {pointer} must be a string array")
    return sorted(set(before))


def canonicalize_reason_classes(document: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize reason_classes arrays under coverage; do not edit other fields."""
    projected = copy.deepcopy(document)
    changes: dict[str, tuple[list[str], list[str]]] = {}

    def visit(node: Any, path: tuple[Any, ...]) -> None:
        if isinstance(node, dict):
            for key, value in list(node.items()):
                child_path = path + (key,)
                if key == "reason_classes":
                    if not isinstance(value, dict):
                        raise CompatibilityTransformError(
                            f"reason_classes at {json_pointer(child_path)} must be an object"
                        )
                    for symbol, reasons in list(value.items()):
                        pointer = json_pointer(child_path + (symbol,))
                        canonical = _canonical_array(reasons, pointer)
                        if reasons != canonical:
                            changes[pointer] = (list(reasons), canonical)
                            value[symbol] = canonical
                else:
                    visit(value, child_path)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, path + (index,))

    if "coverage" not in projected or not isinstance(projected["coverage"], dict):
        raise CompatibilityTransformError("coverage object is required")
    visit(projected["coverage"], ("coverage",))
    return projected


def _changed_pointers(before: Any, after: Any, path: tuple[Any, ...] = ()) -> set[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        keys = set(before) | set(after)
        changed: set[str] = set()
        for key in keys:
            if key not in before or key not in after:
                changed.add(json_pointer(path + (key,)))
            else:
                changed |= _changed_pointers(before[key], after[key], path + (key,))
        return changed
    if isinstance(before, list) and isinstance(after, list):
        if before == after:
            return set()
        return {json_pointer(path)}
    if before != after:
        return {json_pointer(path)}
    return set()


def verify_allowed_diff(before: Mapping[str, Any], after: Mapping[str, Any], allowed_pointers: Sequence[str]) -> None:
    actual = _changed_pointers(before, after)
    allowed = set(allowed_pointers)
    if actual != allowed:
        raise CompatibilityTransformError(
            f"field diff mismatch; actual={sorted(actual)!r}, allowed={sorted(allowed)!r}"
        )
    for pointer in actual:
        if not pointer.startswith("/coverage/") or "/reason_classes/" not in pointer:
            raise CompatibilityTransformError(f"non-diagnostic field changed: {pointer}")


def _get_pointer(document: Any, pointer: str) -> Any:
    current = document
    for raw_part in pointer.lstrip("/").split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current[part]
    return current


def transform_snapshot_bytes(
    raw: bytes,
    *,
    original_path: str,
    expected_original_blob_sha: str,
    original_schema_sha: str,
    original_schema_validator: Any,
    current_schema_sha: str,
    current_schema_validator: Any,
    transformed_at: str | None = None,
    store_repository: str = "yuatom/stock-market-data-store",
    store_commit: str = "UNPINNED",
    transformer_commit: str | None = None,
) -> tuple[bytes, dict[str, Any]]:
    path = _safe_snapshot_path(original_path)
    if not HEX40.fullmatch(expected_original_blob_sha):
        raise CompatibilityTransformError("expected original Git blob must be a lowercase 40-hex SHA")
    if not HEX40.fullmatch(original_schema_sha) or not HEX40.fullmatch(current_schema_sha):
        raise CompatibilityTransformError("schema blob identities must be lowercase 40-hex SHAs")
    if transformer_commit is not None and not HEX40.fullmatch(transformer_commit):
        raise CompatibilityTransformError("transformer commit must be a lowercase 40-hex SHA")
    if git_blob_sha_bytes(raw) != expected_original_blob_sha:
        raise CompatibilityTransformError("raw bytes do not match expected original Git blob SHA")
    try:
        original = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompatibilityTransformError(f"source Snapshot is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(original, dict):
        raise CompatibilityTransformError("source Snapshot must be a JSON object")

    original_errors = list(original_schema_validator.iter_errors(original))
    before_errors = list(current_schema_validator.iter_errors(original))
    if not before_errors:
        raise CompatibilityTransformError("source object is already current-schema valid; not a repair target")
    unsupported = [
        e for e in before_errors
        if e.validator != "uniqueItems"
        or len(_error_path(e)) != 3
        or _error_path(e)[:2] != ("coverage", "reason_classes")
    ]
    if unsupported:
        summary = [(str(e.validator), json_pointer(_error_path(e))) for e in unsupported]
        raise CompatibilityTransformError(f"unsupported current-schema errors: {summary!r}")

    target_pointers = sorted({json_pointer(_error_path(e)) for e in before_errors})
    projected = copy.deepcopy(original)
    changes: dict[str, tuple[list[str], list[str]]] = {}
    for pointer in target_pointers:
        before_values = _get_pointer(original, pointer)
        after_values = _canonical_array(before_values, pointer)
        if before_values == after_values:
            raise CompatibilityTransformError(f"uniqueItems error has no duplicate/order repair at {pointer}")
        changes[pointer] = (list(before_values), after_values)
        _set_pointer(projected, pointer, after_values)
    verify_allowed_diff(original, projected, target_pointers)

    after_errors = list(current_schema_validator.iter_errors(projected))
    if after_errors:
        detail = [(str(e.validator), json_pointer(_error_path(e)), e.message) for e in after_errors]
        raise CompatibilityTransformError(f"derived Snapshot remains current-schema invalid: {detail!r}")
    derived_raw = canonical_bytes(projected)
    transformed_at = transformed_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        parsed_time = datetime.fromisoformat(transformed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CompatibilityTransformError("transformed_at must be an ISO-8601 timestamp") from exc
    if parsed_time.tzinfo is None:
        raise CompatibilityTransformError("transformed_at must include a timezone")

    before_map = {pointer: changes[pointer][0] for pointer in target_pointers}
    after_map = {pointer: changes[pointer][1] for pointer in target_pointers}
    manifest = {
        "manifest_schema_version": 1,
        "original_path": path,
        "original_blob_sha": expected_original_blob_sha,
        "original_schema_sha": original_schema_sha,
        "current_schema_sha": current_schema_sha,
        "original_raw_schema_status": "FAIL" if original_errors else "PASS",
        "current_compatibility_status_before": "FAIL",
        "derived_content_sha256": hashlib.sha256(derived_raw).hexdigest(),
        "transform_version": TRANSFORM_VERSION,
        "transformed_at": transformed_at,
        "historical_first_known_time": "NOT_ASSERTED",
        "exact_changed_json_pointers": target_pointers,
        "before_values": before_map,
        "after_values": after_map,
        "semantic_preservation_proof": {
            "duplicate_free_sorted_unique_arrays": True,
            "reason_class_sets_preserved": all(set(before_map[p]) == set(after_map[p]) for p in target_pointers),
            "all_non_reason_fields_equal": True,
            "original_raw_bytes_sha256": hashlib.sha256(raw).hexdigest(),
            "original_raw_git_blob_sha": git_blob_sha_bytes(raw),
            "transformed_time_is_not_historical_publication_time": True,
        },
        "current_compatibility_status_after": "PASS",
        "provenance": {
            "store_repository": store_repository,
            "store_commit": store_commit,
            "transformer_repository": "yuatom/stock-market-data",
            "transformer_commit": transformer_commit or "UNPINNED",
        },
        "non_authoritative": True,
    }
    return derived_raw, manifest


def _set_pointer(document: Any, pointer: str, value: Any) -> None:
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.lstrip("/").split("/")]
    current = document
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    if isinstance(current, list):
        current[int(parts[-1])] = value
    else:
        current[parts[-1]] = value


def _git_bytes(repo: Path, revision: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{revision}:{path}"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def _write_create_only(path: Path, payload: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(payload)
        return True
    except FileExistsError:
        if path.read_bytes() != payload:
            raise CompatibilityTransformError(f"refusing to replace different derived artifact: {path}")
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-repo", type=Path, required=True)
    parser.add_argument("--store-commit", required=True)
    parser.add_argument("--object-results", type=Path, required=True)
    parser.add_argument("--original-schema", type=Path, required=True)
    parser.add_argument("--original-schema-sha", required=True)
    parser.add_argument("--current-schema", type=Path, required=True)
    parser.add_argument("--current-schema-sha", required=True)
    parser.add_argument("--manifest-schema", type=Path, default=Path(__file__).resolve().parents[1] / "schemas/legacy-snapshot-compatibility-manifest.schema.json")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--transformed-at", required=True)
    parser.add_argument("--transformer-commit", required=True)
    args = parser.parse_args(argv)
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as exc:
        parser.error(f"Draft 2020-12 jsonschema dependency is required: {exc}")
    original_schema_raw = args.original_schema.read_bytes()
    current_schema_raw = args.current_schema.read_bytes()
    if git_blob_sha_bytes(original_schema_raw) != args.original_schema_sha:
        raise CompatibilityTransformError("original schema file does not match expected Git blob SHA")
    if git_blob_sha_bytes(current_schema_raw) != args.current_schema_sha:
        raise CompatibilityTransformError("current schema file does not match expected Git blob SHA")
    original_schema = json.loads(original_schema_raw.decode("utf-8"))
    current_schema = json.loads(current_schema_raw.decode("utf-8"))
    manifest_schema = json.loads(args.manifest_schema.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(original_schema)
    Draft202012Validator.check_schema(current_schema)
    Draft202012Validator.check_schema(manifest_schema)
    original_validator = Draft202012Validator(original_schema, format_checker=FormatChecker())
    current_validator = Draft202012Validator(current_schema, format_checker=FormatChecker())
    manifest_validator = Draft202012Validator(manifest_schema, format_checker=FormatChecker())
    rows = [json.loads(line) for line in args.object_results.read_text(encoding="utf-8").splitlines() if line]
    targets = [row for row in rows if row.get("validation_status") == "FAIL"]
    emitted = []
    for row in targets:
        original_path = row["store_path"]
        source = _git_bytes(args.store_repo, args.store_commit, original_path)
        actual = git_blob_sha_bytes(source)
        if actual != row["store_blob"]:
            raise CompatibilityTransformError(f"object report blob mismatch for {original_path}")
        derived, manifest = transform_snapshot_bytes(
            source,
            original_path=original_path,
            expected_original_blob_sha=actual,
            original_schema_sha=args.original_schema_sha,
            original_schema_validator=original_validator,
            current_schema_sha=args.current_schema_sha,
            current_schema_validator=current_validator,
            transformed_at=args.transformed_at,
            store_commit=args.store_commit,
            transformer_commit=args.transformer_commit,
        )
        rel = PurePosixPath(original_path)
        out = args.output_root / "derived" / TRANSFORM_VERSION / Path(*rel.parts)
        manifest_path = out.with_suffix(out.suffix + ".manifest.json")
        manifest_validator.validate(manifest)
        manifest_raw = canonical_bytes(manifest)
        if out.exists() and out.read_bytes() != derived:
            raise CompatibilityTransformError(f"refusing to replace different derived artifact: {out}")
        if manifest_path.exists() and manifest_path.read_bytes() != manifest_raw:
            raise CompatibilityTransformError(f"refusing to replace different derived artifact: {manifest_path}")
        created = _write_create_only(out, derived)
        manifest_created = _write_create_only(manifest_path, manifest_raw)
        emitted.append({"original_path": original_path, "derived_path": str(out), "manifest_path": str(manifest_path), "derived_content_sha256": manifest["derived_content_sha256"], "created": created, "manifest_created": manifest_created, "status": manifest["current_compatibility_status_after"]})
    print(json.dumps({"transform_version": TRANSFORM_VERSION, "target_count": len(targets), "emitted_count": len(emitted), "derived": emitted}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CompatibilityTransformError as exc:
        print(f"legacy_snapshot_compat: {exc}", file=sys.stderr)
        raise SystemExit(2)
