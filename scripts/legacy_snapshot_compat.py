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
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

TRANSFORM_VERSION = "legacy-reason-classes-canonicalize-v2"
EXPECTED_REPORT_ROWS = 499
EXPECTED_REPAIR_TARGETS = 42
SNAPSHOT_PREFIX = "data/market-data/snapshots/"
HEX40 = re.compile(r"^[0-9a-f]{40}$")


class CompatibilityTransformError(ValueError):
    """Raised when an input cannot be transformed without changing facts."""


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CompatibilityTransformError(f"duplicate JSON member name: {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise CompatibilityTransformError(f"nonfinite JSON literal is forbidden: {value}")


def strict_json_loads(raw: str | bytes) -> Any:
    """Reject duplicate JSON object member names at all nested levels."""
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return json.loads(
            text, object_pairs_hook=_unique_json_object,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompatibilityTransformError(f"invalid UTF-8/JSON input: {exc}") from exc


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
    if (
        not isinstance(value, str) or not value or "\\" in value
        or value.startswith("/") or "\x00" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise CompatibilityTransformError("original path must be a canonical relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts) or PurePosixPath(value).as_posix() != value:
        raise CompatibilityTransformError("original path has noncanonical/unsafe components")
    path = PurePosixPath(value)
    if (
        len(parts) < 7 or not value.startswith(SNAPSHOT_PREFIX)
        or path.suffix != ".json"
        or any(part == "latest" or part.startswith("latest.") for part in parts)
    ):
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


def _git_checked(repo: Path, *arguments: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout
    except (subprocess.CalledProcessError, OSError) as exc:
        raise CompatibilityTransformError(f"Git provenance command failed: {arguments!r}") from exc


def _verify_transformer_identity(transformer_commit: str | None) -> str:
    """Prove the executing script equals the pinned actual checkout commit."""
    if not isinstance(transformer_commit, str) or not HEX40.fullmatch(transformer_commit):
        raise CompatibilityTransformError("verified transformer commit SHA is required")
    script = Path(__file__).resolve(strict=True)
    root = script.parents[1]
    if script != root / "scripts" / "legacy_snapshot_compat.py":
        raise CompatibilityTransformError("executing transformer is not in canonical scripts path")
    actual_root = _git_checked(root, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    if Path(actual_root).resolve(strict=True) != root:
        raise CompatibilityTransformError("transformer is not in the claimed repository root")
    if _git_checked(root, "rev-parse", "HEAD").decode("ascii").strip() != transformer_commit:
        raise CompatibilityTransformError("transformer commit differs from checkout HEAD")
    if _git_checked(root, "cat-file", "-t", transformer_commit).decode("ascii").strip() != "commit":
        raise CompatibilityTransformError("transformer SHA is not a Git commit")
    frozen_bytes = _git_checked(root, "show", f"{transformer_commit}:scripts/legacy_snapshot_compat.py")
    if frozen_bytes != script.read_bytes():
        raise CompatibilityTransformError("executed transformer script differs from pinned Git bytes")
    return transformer_commit


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
    verified_transformer_commit = _verify_transformer_identity(transformer_commit)
    if git_blob_sha_bytes(raw) != expected_original_blob_sha:
        raise CompatibilityTransformError("raw bytes do not match expected original Git blob SHA")
    original = strict_json_loads(raw)
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
            "transformer_commit": verified_transformer_commit,
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
    return _git_checked(repo, "show", f"{revision}:{path}")


def _load_pinned_targets(
    report_raw: bytes, inventory_raw: bytes, expected_inventory_sha256: str,
    *, store_commit: str, current_schema_sha: str,
    expected_rows: int = EXPECTED_REPORT_ROWS,
    expected_targets: int = EXPECTED_REPAIR_TARGETS,
) -> list[dict[str, Any]]:
    """Validate one independently pinned 499-row result and its exact 42 FAIL targets."""
    if (
        not isinstance(expected_inventory_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_inventory_sha256)
        or hashlib.sha256(inventory_raw).hexdigest() != expected_inventory_sha256
    ):
        raise CompatibilityTransformError("expected inventory SHA256 is absent or mismatched")
    inventory = strict_json_loads(inventory_raw)
    if not isinstance(inventory, dict) or set(inventory) != {
        "schema_version", "store_commit", "current_schema_sha",
        "report_sha256", "report_rows", "target_count", "targets",
    }:
        raise CompatibilityTransformError("expected inventory fields are missing/unknown")
    if inventory_raw != canonical_bytes(inventory):
        raise CompatibilityTransformError("expected inventory must be canonical JSON bytes")
    if (
        type(inventory["schema_version"]) is not int or inventory["schema_version"] != 1
        or inventory["store_commit"] != store_commit
        or inventory["current_schema_sha"] != current_schema_sha
        or inventory["report_sha256"] != hashlib.sha256(report_raw).hexdigest()
        or type(inventory["report_rows"]) is not int
        or inventory["report_rows"] != expected_rows
        or type(inventory["target_count"]) is not int
        or inventory["target_count"] != expected_targets
    ):
        raise CompatibilityTransformError("expected inventory contract/identity/count mismatch")
    lines = report_raw.splitlines()
    if len(lines) != expected_rows or any(not line.strip() for line in lines):
        raise CompatibilityTransformError("report row closure incomplete or contains blank rows")
    rows_by_path: dict[str, dict[str, Any]] = {}
    fail_by_path: dict[str, str] = {}
    for line in lines:
        row = strict_json_loads(line)
        if not isinstance(row, dict):
            raise CompatibilityTransformError("object result must be a JSON object")
        status = row.get("validation_status")
        if status not in ("PASS", "FAIL"):
            raise CompatibilityTransformError("object result has absent/unknown validation_status")
        path = _safe_snapshot_path(row.get("store_path"))
        blob = row.get("store_blob")
        if not isinstance(blob, str) or not HEX40.fullmatch(blob):
            raise CompatibilityTransformError(f"invalid object report blob at {path}")
        if path in rows_by_path:
            raise CompatibilityTransformError(f"duplicate report Store path: {path}")
        rows_by_path[path] = row
        if status == "FAIL":
            fail_by_path[path] = blob
    expected = inventory["targets"]
    if not isinstance(expected, list) or len(expected) != expected_targets:
        raise CompatibilityTransformError("expected target list is missing/incomplete")
    target_map: dict[str, str] = {}
    for item in expected:
        if not isinstance(item, dict) or set(item) != {"store_path", "store_blob"}:
            raise CompatibilityTransformError("invalid expected target identity")
        path = _safe_snapshot_path(item["store_path"])
        blob = item["store_blob"]
        if not isinstance(blob, str) or not HEX40.fullmatch(blob) or path in target_map:
            raise CompatibilityTransformError("duplicate/invalid expected target")
        target_map[path] = blob
    if (
        len(fail_by_path) != expected_targets or fail_by_path != target_map
        or list(target_map) != sorted(target_map)
    ):
        raise CompatibilityTransformError("current-Schema FAIL targets differ from pinned complete inventory")
    return [rows_by_path[path] for path in sorted(target_map)]


def _trusted_output_root(root: Path, store_repo: Path) -> Path:
    if not root.is_dir() or root.is_symlink():
        raise CompatibilityTransformError("private scratch output-root must exist and not be a symlink")
    try:
        output = root.resolve(strict=True)
        store = store_repo.resolve(strict=True)
        compute = Path(__file__).resolve(strict=True).parents[1]
    except OSError as exc:
        raise CompatibilityTransformError("cannot resolve output/source roots") from exc
    if any(
        output == repo or output.is_relative_to(repo) or repo.is_relative_to(output)
        for repo in (store, compute)
    ):
        raise CompatibilityTransformError("private output-root overlaps the Compute or Store checkout")
    return output


def _write_create_only(root: Path, relative: Path, payload: bytes) -> bool:
    """Write under private root with O_NOFOLLOW on each component and file."""
    parts = relative.parts
    if relative.is_absolute() or not parts or any(p in {"", ".", ".."} for p in parts):
        raise CompatibilityTransformError("unsafe derived relative output path")
    parent_fd = None
    try:
        parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for component in parts[:-1]:
            try:
                os.mkdir(component, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            next_fd = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
        try:
            output_fd = os.open(
                parts[-1], os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600, dir_fd=parent_fd,
            )
        except FileExistsError:
            existing_fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd,
            )
            with os.fdopen(existing_fd, "rb") as stream:
                if stream.read() != payload:
                    raise CompatibilityTransformError(
                        f"refusing to replace different derived artifact: {relative}"
                    )
            return False
        with os.fdopen(output_fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return True
    except OSError as exc:
        raise CompatibilityTransformError(f"unsafe/inaccessible private scratch output: {relative}") from exc
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-repo", type=Path, required=True)
    parser.add_argument("--store-commit", required=True)
    parser.add_argument("--object-results", type=Path, required=True)
    parser.add_argument("--expected-inventory", type=Path, required=True)
    parser.add_argument("--expected-inventory-sha256", required=True)
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
    if not isinstance(args.store_commit, str) or not HEX40.fullmatch(args.store_commit):
        raise CompatibilityTransformError("Store commit must be an exact 40-hex Git SHA")
    _verify_transformer_identity(args.transformer_commit)
    root = _trusted_output_root(args.output_root, args.store_repo)
    if _git_checked(args.store_repo, "rev-parse", "HEAD").decode("ascii").strip() != args.store_commit:
        raise CompatibilityTransformError("Store checkout HEAD must equal the pinned Store commit")
    if _git_checked(args.store_repo, "cat-file", "-t", args.store_commit).decode("ascii").strip() != "commit":
        raise CompatibilityTransformError("Store pin does not name a Git commit")
    original_schema_raw = args.original_schema.read_bytes()
    current_schema_raw = args.current_schema.read_bytes()
    if git_blob_sha_bytes(original_schema_raw) != args.original_schema_sha:
        raise CompatibilityTransformError("original schema file does not match expected Git blob SHA")
    if git_blob_sha_bytes(current_schema_raw) != args.current_schema_sha:
        raise CompatibilityTransformError("current schema file does not match expected Git blob SHA")
    original_schema = strict_json_loads(original_schema_raw)
    current_schema = strict_json_loads(current_schema_raw)
    manifest_schema = strict_json_loads(args.manifest_schema.read_bytes())
    Draft202012Validator.check_schema(original_schema)
    Draft202012Validator.check_schema(current_schema)
    Draft202012Validator.check_schema(manifest_schema)
    original_validator = Draft202012Validator(original_schema, format_checker=FormatChecker())
    current_validator = Draft202012Validator(current_schema, format_checker=FormatChecker())
    manifest_validator = Draft202012Validator(manifest_schema, format_checker=FormatChecker())
    targets = _load_pinned_targets(
        args.object_results.read_bytes(), args.expected_inventory.read_bytes(),
        args.expected_inventory_sha256,
        store_commit=args.store_commit, current_schema_sha=args.current_schema_sha,
    )
    pending = []
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
        rel = Path("derived") / TRANSFORM_VERSION / Path(*PurePosixPath(original_path).parts)
        manifest_rel = rel.with_suffix(rel.suffix + ".manifest.json")
        manifest_validator.validate(manifest)
        manifest_raw = canonical_bytes(manifest)
        pending.append((original_path, rel, manifest_rel, derived, manifest_raw, manifest["derived_content_sha256"]))
    if len(pending) != EXPECTED_REPAIR_TARGETS:
        raise CompatibilityTransformError("pre-write target closure is not exactly 42")
    emitted = []
    for original_path, rel, manifest_rel, derived, manifest_raw, content_sha in pending:
        created = _write_create_only(root, rel, derived)
        manifest_created = _write_create_only(root, manifest_rel, manifest_raw)
        emitted.append({
            "original_path": original_path, "derived_path": str(root / rel),
            "manifest_path": str(root / manifest_rel),
            "derived_content_sha256": content_sha,
            "created": created, "manifest_created": manifest_created, "status": "PASS",
        })
    if len(emitted) != EXPECTED_REPAIR_TARGETS:
        raise CompatibilityTransformError("derived output closure is not exactly 42")
    print(json.dumps({
        "transform_version": TRANSFORM_VERSION, "target_count": len(targets),
        "emitted_count": len(emitted), "inventory_sha256": args.expected_inventory_sha256,
        "derived": emitted,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CompatibilityTransformError as exc:
        print(f"legacy_snapshot_compat: {exc}", file=sys.stderr)
        raise SystemExit(2)
