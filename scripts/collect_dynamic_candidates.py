#!/usr/bin/env python3
"""Collect request-scoped market facts for dynamic research objects.

Dynamic membership is bounded to one immutable request and is never a research
priority or opportunity-qualification authority. Personal-membership requests
are a distinct data-acquisition purpose and never mutate durable membership or
the fixed collection universe. Open15/Open60 still forbid new radar discovery.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import yaml

import market_data_collectors as collectors
import market_data_store as store
import dynamic_daily_response as daily_response
from market_data_collection import collect_regular_window


class DynamicCandidateCollectionError(RuntimeError):
    pass


HEX40 = re.compile(r"^[0-9a-f]{40}$")
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
ALLOWED_STAGES = {"open_15m", "open_30m", "open_60m", "close"}
ALLOWED_PURPOSES = {"carryover_validation", "opportunity_discovery", "personal_membership"}


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DynamicCandidateCollectionError(f"{path} must contain a YAML mapping")
    return value


def _load_request(path: Path, *, expected_contract_sha: str | None = None) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DynamicCandidateCollectionError("request must be a JSON object")
    required = {
        "schema_version", "request_id", "requested_at", "trade_date", "stage", "request_purpose",
        "research_repository", "research_repository_commit_sha", "market_data_contract_sha", "candidate_symbols",
        "transaction_id", "personal_state_proof", "research_universe_resolution_status",
    }
    missing = sorted(required - set(value))
    if missing:
        raise DynamicCandidateCollectionError(f"request missing fields: {missing}")
    allowed = set(required)
    extra = sorted(set(value) - allowed)
    if extra:
        raise DynamicCandidateCollectionError(f"request has unsupported fields: {extra}")
    if value.get("schema_version") != 1:
        raise DynamicCandidateCollectionError("schema_version must be 1")
    if value.get("research_repository") != "yuatom/stock-dairy":
        raise DynamicCandidateCollectionError("research_repository is unauthorized")
    stage = str(value.get("stage") or "")
    purpose = str(value.get("request_purpose") or "")
    if stage not in ALLOWED_STAGES:
        raise DynamicCandidateCollectionError("stage must be open_15m, open_30m, open_60m or close")
    if purpose not in ALLOWED_PURPOSES:
        raise DynamicCandidateCollectionError("invalid request_purpose")
    if stage in {"open_15m", "open_60m"} and purpose not in {"carryover_validation", "personal_membership"}:
        raise DynamicCandidateCollectionError(f"{stage} dynamic collection forbids new opportunity discovery")
    for field in ("research_repository_commit_sha", "market_data_contract_sha"):
        if not HEX40.fullmatch(str(value.get(field) or "")):
            raise DynamicCandidateCollectionError(f"{field} must be a 40-char SHA")
    if expected_contract_sha and value.get("market_data_contract_sha") != expected_contract_sha:
        raise DynamicCandidateCollectionError("request market_data_contract_sha does not match collector contract")
    if value.get("research_universe_resolution_status") != "resolved":
        raise DynamicCandidateCollectionError("research_universe_resolution_status must be resolved")
    proof = value.get("personal_state_proof")
    if not isinstance(proof, dict):
        raise DynamicCandidateCollectionError("personal_state_proof must be an object")
    proof_required = {
        "dynamic_state_read_sha", "blob_sha", "state_version", "authority_state",
        "content_hash_status", "content_sha256",
    }
    if set(proof) != proof_required:
        raise DynamicCandidateCollectionError("personal_state_proof fields are incomplete or unsupported")
    for field in ("dynamic_state_read_sha", "blob_sha"):
        if not HEX40.fullmatch(str(proof.get(field) or "")):
            raise DynamicCandidateCollectionError(f"personal_state_proof {field} must be a 40-char SHA")
    if not isinstance(proof.get("state_version"), int) or proof["state_version"] < 1:
        raise DynamicCandidateCollectionError("personal_state_proof state_version must be positive")
    if proof.get("authority_state") not in {"shadow_pre_cutover", "canonical"}:
        raise DynamicCandidateCollectionError("personal_state_proof authority_state is invalid")
    hash_status = proof.get("content_hash_status")
    if hash_status == "canonical_sha256":
        if not re.fullmatch(r"[0-9a-f]{64}", str(proof.get("content_sha256") or "")):
            raise DynamicCandidateCollectionError("canonical personal_state_proof requires content_sha256")
    elif hash_status == "unavailable_no_script":
        if proof.get("content_sha256") is not None:
            raise DynamicCandidateCollectionError("unavailable_no_script requires null content_sha256")
    else:
        raise DynamicCandidateCollectionError("personal_state_proof content_hash_status is invalid")
    transaction_id = value.get("transaction_id")
    if not isinstance(transaction_id, str) or not transaction_id.strip():
        raise DynamicCandidateCollectionError("transaction_id must be a non-empty string")
    symbols = value.get("candidate_symbols")
    if not isinstance(symbols, list) or not (1 <= len(symbols) <= 8):
        raise DynamicCandidateCollectionError("candidate_symbols must contain 1-8 symbols")
    normalized = [str(symbol).upper() for symbol in symbols]
    if len(normalized) != len(set(normalized)) or any(not SYMBOL.fullmatch(symbol) for symbol in normalized):
        raise DynamicCandidateCollectionError("candidate_symbols must be unique valid uppercase tickers")
    value["candidate_symbols"] = normalized
    try:
        datetime.fromisoformat(str(value["requested_at"]).replace("Z", "+00:00"))
        datetime.fromisoformat(str(value["trade_date"]))
    except ValueError as exc:
        raise DynamicCandidateCollectionError("requested_at or trade_date is invalid") from exc
    return value


class DynamicDailyOverlapConflict(store.MarketDataConflict):
    """Trusted local comparison only, not arbitrary provider error text."""
    def __init__(self,symbol,day):
        super().__init__('provider historical revision detected')
        self.symbol=symbol;self.day=day


def _append_price_only_history(*, symbol, eligible, store_root, trade_date, target_records, evidence_path, evidence_digest):
    """Existing overlap classifier/quarantine owner; never replace a historical row."""
    index_path=collectors._series_index_path(store_root,collectors.TWELVE_PROVIDER,symbol)
    existing=[];blocked=False
    if len({r['trade_date'] for r in eligible})!=len(eligible) or any(
            not collectors.classify_daily_overlap(dict(r),dict(r))['price_eligible'] for r in eligible):
        raise store.MarketDataConflict('daily response quality invalid')
    if index_path.exists():
        index=json.loads(index_path.read_text())
        expected=dict(provider=collectors.TWELVE_PROVIDER,symbol=symbol,asset_class='stocks',session='regular',
                      interval='1d',series_semantics='daily_regular_ohlcv',adjustment_semantics='provider_reported',identity_effective_from=None)
        if any(index.get(k)!=v for k,v in expected.items()):
            raise store.MarketDataConflict('dynamic daily series identity mismatch')
        existing=store.read_daily_series(store_root,provider=collectors.TWELVE_PROVIDER,symbol=symbol)
        segments=index.get('lineage_segments',[])
        if not isinstance(segments,list) or any(not isinstance(x,dict) for x in segments):
            raise store.MarketDataConflict('stored lineage schema invalid')
        blocked=any(x.get('volume_policy')=='BLOCKED_UNRESOLVED' or x.get('ingest_kind')=='collector_price_only_revision_v1' for x in segments)
    old={r['trade_date']:r for r in existing}
    comparisons=[dict(trade_date=r['trade_date'],**collectors.classify_daily_overlap(dict(old[r['trade_date']]),dict(r))) for r in eligible if r['trade_date'] in old]
    actual_volume_conflict=any('volume' in c['changed_fields'] for c in comparisons)
    if actual_volume_conflict:
        store.quarantine_daily_volume(store_root,provider=collectors.TWELVE_PROVIDER,symbol=symbol,
            evidence_path=evidence_path,evidence_sha256=evidence_digest,observed_trade_date=trade_date)
        blocked=True
    rejected=[c for c in comparisons if not c['price_eligible']]
    if rejected:
        if any(c['classification']!='PRICE_REVISION' for c in rejected):
            raise store.MarketDataConflict('historical quality or metadata comparison rejected')
        raise DynamicDailyOverlapConflict(symbol,rejected[0]['trade_date'])
    # Never fill an internal gap or expand a historical prefix silently.
    if existing and any(r['trade_date']<=existing[-1]['trade_date'] and r['trade_date'] not in old for r in eligible):
        raise store.MarketDataConflict('dynamic daily internal or prefix insertion rejected')
    new=[r for r in eligible if not existing or r['trade_date']>existing[-1]['trade_date']]
    lineage=dict(ingest_kind='dynamic_research_object_history',ingest_trade_date=trade_date,history_outputsize=target_records)
    if blocked:lineage.update(ingest_kind='collector_price_only_revision_v1',volume_policy='BLOCKED_UNRESOLVED',normalized_evidence_path=evidence_path,normalized_evidence_sha256=evidence_digest)
    if new:store.append_daily_bars(store_root,provider=collectors.TWELVE_PROVIDER,symbol=symbol,asset_class='stocks',bars=new,
        identity_effective_from=None,series_semantics='daily_regular_ohlcv',adjustment_semantics='provider_reported',lineage=lineage)


def _ensure_daily_history(*, symbols: Sequence[str], trade_date: str, store_root: Path, config: dict[str, Any], access: dict[str, Any], target_records: int, diagnostics=None) -> tuple[list[str], list[dict[str, str]]]:
    diagnostics=diagnostics if diagnostics is not None else {}
    diagnostics.update(requests_attempted=0,local_budget_debits=0,provider_charge_status='NOT_ESTABLISHED',
        remote_predebit_established=False,response_retention='raw_bytes_before_strict_parse_and_normalized_evidence',stopped=False,required_paths=[])
    key = os.environ.get(str((access.get("twelve_data_basic") or {}).get("api_key_env") or "TWELVE_DATA_API_KEY"))
    policy=config['collector']['twelve_data_daily'].get('overlap_revision_policy')
    if policy not in (None,'price_only_volume_blocked_v1'):
        diagnostics['stopped']=True
        return [],[dict(symbol=symbols[0] if symbols else '',reason='unsupported_daily_revision_policy')]
    price_only=policy=='price_only_volume_blocked_v1'
    if not key:
        diagnostics['stopped']=True
        return [], [{"symbol": symbol, "reason": "TWELVE_DATA_API_KEY_missing"} for symbol in symbols]
    budget = config["collector"]["budgets"]["twelve_data_basic"]
    available=[];failures=[];required=set()
    for symbol_index,symbol in enumerate(symbols):
        phase='budget_check'
        try:
            store.assert_legacy_basis(store_root, provider=collectors.TWELVE_PROVIDER, symbol=symbol)
            collectors.wait_for_twelve_budget(store_root,trade_date,per_day=int(budget['hard_credits_per_day']),per_minute=int(budget['hard_credits_per_minute']))
            phase='provider_fetch';diagnostics['requests_attempted']+=1;diagnostics['provider_charge_status']='UNKNOWN'
            raw=daily_response.fetch_raw(symbol,key,access,outputsize=target_records)
            phase='raw_response_retention'
            raw_digest=hashlib.sha256(raw).hexdigest()
            raw_path=f'collector-state/{trade_date[:7]}/dynamic-daily-{symbol}-{raw_digest}.raw'
            destination=store_root/raw_path;destination.parent.mkdir(parents=True,exist_ok=True)
            if destination.exists():
                if destination.is_symlink() or destination.read_bytes()!=raw:raise store.MarketDataConflict('raw evidence identity mismatch')
            else:
                with destination.open('xb') as handle:handle.write(raw)
            required.add(raw_path)
            diagnostics.setdefault('raw_responses',[]).append(dict(symbol=symbol,path=raw_path,sha256=raw_digest,bytes=len(raw)))
            phase='strict_daily_parse'
            fetched=daily_response.parse(raw,symbol=symbol,outputsize=target_records)
            # Retention is normalized parser output, not original HTTP/metadata.
            if price_only:
                phase='normalized_response_retention'
                payload=store.canonical_bytes(dict(schema_version=1,kind='normalized_daily_price_only_evidence',
                    symbol=symbol,execution_day=trade_date,records=fetched,volume_policy='BLOCKED_UNRESOLVED'))
                evidence_digest=hashlib.sha256(payload).hexdigest()
                evidence_path=f'collector-state/{trade_date[:7]}/price-only-{evidence_digest}.json'
                target=store_root/evidence_path;target.parent.mkdir(parents=True,exist_ok=True)
                if target.exists():
                    if target.is_symlink() or target.read_bytes()!=payload:raise store.MarketDataConflict('evidence identity mismatch')
                else:
                    with target.open('xb') as handle:handle.write(payload)
                required.add(evidence_path)
            # Existing success-fetch accounting order retained. This is local only;
            # outer publication, not this function, establishes remote durability.
            phase='local_budget_debit'
            collectors.consume_twelve_credit(store_root,trade_date,amount=1,per_day=int(budget['hard_credits_per_day']),per_minute=int(budget['hard_credits_per_minute']))
            diagnostics['local_budget_debits']+=1
            diagnostics['provider_charge_status']='LOCAL_REQUESTS_ACCOUNTED_PROVIDER_BILLING_NOT_VERIFIED'
            eligible=[r for r in fetched if str(r.get('trade_date') or '')<trade_date]
            if not eligible:raise store.MarketDataConflict('no_cutoff_valid_daily_history')
            phase='immutable_history_qualification'
            if price_only:
                _append_price_only_history(symbol=symbol,eligible=eligible,store_root=store_root,trade_date=trade_date,
                    target_records=target_records,evidence_path=evidence_path,evidence_digest=evidence_digest)
            else:
                store.append_daily_bars(store_root,provider=collectors.TWELVE_PROVIDER,symbol=symbol,asset_class='stocks',bars=eligible,
                    identity_effective_from=None,series_semantics='daily_regular_ohlcv',adjustment_semantics='provider_reported',
                    lineage=dict(ingest_kind='dynamic_research_object_history',ingest_trade_date=trade_date,history_outputsize=target_records))
            available.append(symbol)
        except Exception as exc:
            reason=type(exc).__name__
            if not price_only and type(exc) is store.MarketDataConflict and phase=='immutable_history_qualification':
                match=re.fullmatch(rf'conflicting bar for {re.escape(symbol)} (\d{{4}}-\d{{2}}-\d{{2}})',str(exc))
                if match:
                    reason=f'MarketDataConflict:conflicting bar for {symbol} {match.group(1)}'
            if type(exc) is DynamicDailyOverlapConflict:
                reason=f'MarketDataConflict:conflicting bar for {exc.symbol} {exc.day}'
            failures.append(dict(symbol=symbol,reason=reason,phase=phase))
            if type(exc) is DynamicDailyOverlapConflict:
                diagnostics.setdefault('known_price_rejected_symbols',[]).append(symbol)
                continue
            diagnostics.update(stopped=True,stop_phase=phase,failed_symbol=symbol,
                not_attempted_symbols=list(symbols[symbol_index+1:]),automatic_retry_allowed=False)
            break
        finally:
            # Bind actual bytes (including a quarantine written before rejection).
            directory=collectors._series_index_path(store_root,collectors.TWELVE_PROVIDER,symbol).parent
            if directory.exists():
                required.update(str(p.relative_to(store_root)) for p in directory.glob('*.json') if p.is_file())
            ledger=collectors._budget_path(store_root,trade_date)
            if ledger.is_file():required.add(str(ledger.relative_to(store_root)))
            diagnostics['required_paths']=sorted(required)
    return available,failures


def collect_request(*, request: dict[str, Any], store_root: Path, store_config_path: Path, access_path: Path, dynamic_config_path: Path) -> dict[str, Any]:
    config = collectors.load_yaml(store_config_path)
    access = collectors.load_yaml(access_path)
    dynamic = _load_yaml(dynamic_config_path)
    symbols = list(request["candidate_symbols"])
    stage = str(request["stage"])
    trade_date = str(request["trade_date"])
    target_records = int((((dynamic.get("collection") or {}).get("daily_series") or {}).get("target_history_sessions") or 256))

    daily_diagnostics={}
    daily_available, daily_failures = _ensure_daily_history(
        symbols=symbols, trade_date=trade_date, store_root=store_root, config=config, access=access, target_records=target_records, diagnostics=daily_diagnostics
    )

    windows = {
        "open_15m": ("09:30", "09:45"),
        "open_30m": ("09:30", "10:00"),
        "open_60m": ("10:00", "10:30"),
        "close": ("15:45", "16:00"),
    }
    start_et, end_et = windows[stage]

    if daily_diagnostics.get('stopped'):
        result=dict(mode=f'discovery_{stage}',status='STOPPED',snapshot_written=False,market_fact_authority=False,
                    automatic_retry_allowed=False,intraday_skipped=True)
    else:
        try:
            result = collect_regular_window(
                mode=f"discovery_{stage}",
                stage=f"discovery_{stage}",
                trade_date=trade_date,
                start_et=start_et,
                end_et=end_et,
                store_root=store_root,
                universe_config={"context_proxies": {}, "sector_etfs": []},
                config=config,
                access=access,
                eligible_universe=[(symbol, "stocks") for symbol in symbols],
            )
        except Exception as exc:
            result=dict(mode=f'discovery_{stage}',status='STOPPED',snapshot_written=False,market_fact_authority=False,
                        automatic_retry_allowed=False,intraday_failure=type(exc).__name__,provider_charge_status='UNKNOWN')
    if daily_failures and result.get('status')=='ok':
        result['status']='partial'
    result.update(
        {
            "request_id": request["request_id"],
            "request_purpose": request["request_purpose"],
            "research_repository_commit_sha": request["research_repository_commit_sha"],
            "market_data_contract_sha": request["market_data_contract_sha"],
            "candidate_symbols": symbols,
            "daily_history_available": sorted(daily_available),
            "daily_history_failures": daily_failures,
            "daily_history_execution": {k:v for k,v in daily_diagnostics.items() if k!="required_paths"},
            "snapshot_stage": f"discovery_{stage}",
            "persistent_collection_membership": False,
            "opportunity_discovery_authority": request["request_purpose"] == "opportunity_discovery",
        }
    )
    state = store_root / "collector-state" / trade_date[:7] / f"{trade_date}-dynamic-candidate-{stage}.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state_payload = dict(result)
    state_payload["schema_version"] = 1
    state_payload["market_fact_authority"] = False
    state_payload["recorded_at"] = datetime.now(timezone.utc).isoformat()
    state.write_text(json.dumps(state_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    result["result_state_path"] = str(state.relative_to(store_root))
    required_paths = [result["result_state_path"], *daily_diagnostics.get("required_paths",[])]
    ledger=collectors._budget_path(store_root,trade_date)
    if ledger.is_file():required_paths.append(str(ledger.relative_to(store_root)))
    if result.get("snapshot_written") and result.get("snapshot_path"):
        required_paths.append(str(result["snapshot_path"]))
    request_token = hashlib.sha256(str(request["request_id"]).encode("utf-8")).hexdigest()[:16]
    manifest = (
        store_root
        / "collector-state"
        / trade_date[:7]
        / f"{trade_date}-dynamic-candidate-{stage}-{request_token}-publication.json"
    )
    def output_ref(relative: str) -> dict[str, str]:
        raw = (store_root / relative).read_bytes()
        header = f"blob {len(raw)}\0".encode("ascii")
        return {
            "path": relative,
            "blob_sha": hashlib.sha1(header + raw).hexdigest(),
        }

    manifest_payload = {
        "schema_version": 1,
        "request_id": request["request_id"],
        "trade_date": trade_date,
        "stage": stage,
        "request_purpose": request["request_purpose"],
        "required_outputs": [output_ref(path) for path in sorted(set(required_paths))],
    }
    manifest.write_text(
        json.dumps(manifest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    result["publication_manifest_path"] = str(manifest.relative_to(store_root))
    result["required_store_outputs"] = manifest_payload["required_outputs"]
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--store-root", default="sources/market-data")
    parser.add_argument("--store-config", default="config/market-data-store.yaml")
    parser.add_argument("--access-config", default="config/market-data-collector-access.yaml")
    parser.add_argument("--dynamic-config", default="config/dynamic-candidate-collection.yaml")
    parser.add_argument("--expected-contract-sha")
    args = parser.parse_args(argv)
    request = _load_request(Path(args.request), expected_contract_sha=args.expected_contract_sha)
    result = collect_request(
        request=request,
        store_root=Path(args.store_root),
        store_config_path=Path(args.store_config),
        access_path=Path(args.access_config),
        dynamic_config_path=Path(args.dynamic_config),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
