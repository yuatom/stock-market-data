"""Pure twelve-target contract/candidate adapter, NOT a production executor.

No approval creation, credentials, provider requests, Git writes, or lock claim.
Deployment requires a separately reviewed shared-lock/admission runtime and
consumer recognition of INGEST_KIND. Original eleven defaults remain closed.
"""
from __future__ import annotations
from datetime import date
import re
from scripts import eleven_symbol_append as core

SYMBOLS = ('GLD',) + core.SYMBOLS
INGEST_KIND = 'twelve_symbol_conditional_append'
PRODUCTION_READY = False

def validate_contract(raw, digest):
    """Validate exact bytes/scope only. This does not establish authorization."""
    try:
        core.require(type(digest) is str and re.fullmatch('[0-9a-f]{64}',digest)
                     and core.sha(raw)==digest)
        value=core.strict_load(raw)
        core.require(type(value) is dict and set(value)=={
            'schema_version','kind','baseline_store_sha','historical_cutoff','targets',
            'request_limit','credit_limit','per_symbol_request_limit'})
        for key,expected in [('schema_version',1),('request_limit',12),
                             ('credit_limit',12),('per_symbol_request_limit',1)]:
            core.require(type(value[key]) is int and value[key]==expected)
        core.require(value['kind']=='twelve_symbol_append_contract')
        core.require(type(value['baseline_store_sha']) is str and
                     re.fullmatch('[0-9a-f]{40}',value['baseline_store_sha']))
        cutoff=value['historical_cutoff']
        core.require(type(cutoff) is str and date.fromisoformat(cutoff).isoformat()==cutoff)
        targets=value['targets']
        core.require(type(targets) is list and all(type(t) is dict for t in targets)
                     and tuple(t.get('symbol') for t in targets)==SYMBOLS)
        for target in targets:
            core.validate_scope(target,target['symbol'])
            core.require(target['addition_dates'][-1]==cutoff)
        return value
    except Exception:
        raise core.RunStopped('TWELVE_CONTRACT_INVALID') from None

def read_series(files, symbol):
    core.require(symbol in SYMBOLS)
    return core.read_series(files,symbol,symbols=SYMBOLS)

def prepare_candidate(base,symbol,raw,request,lineage,blob_pins,scope):
    """Reuse exact base/whole-OHLC/immutable-prefix construction, without IO authority."""
    core.require(symbol in SYMBOLS and type(lineage) is dict and
                 lineage.get('ingest_kind')==INGEST_KIND)
    return core.prepare_qualified_candidate(base,symbol,raw,request,lineage,blob_pins,scope,
                                           symbols=SYMBOLS)

def synthetic_execution_plan(contract_raw,digest,budget_raw,execution_day,minutes):
    """SYNTHETIC execution_day ledger only, never a production trade_date ledger.

    Side-effect-free projection, NOT a debit, lock, grant, or executable approval.

    The eventual runner must recheck maturity, shared remote state and debit each
    request under market-data-collector. Never use this projection as authority.
    """
    contract=validate_contract(contract_raw,digest)
    core.require(type(execution_day) is str and
                 date.fromisoformat(execution_day).isoformat()==execution_day and
                 execution_day>contract['historical_cutoff'])
    core.require(type(minutes) is list and len(minutes)==12 and
                 all(type(m) is str for m in minutes) and minutes==sorted(minutes))
    budget=core.strict_load(budget_raw)
    core.require(type(budget) is dict and type(budget.get('credits')) is int and
                 0<=budget['credits']<=188)
    projected=budget_raw
    steps=[]
    for symbol,minute in zip(SYMBOLS,minutes):
        projected=core.next_budget(projected,execution_day,minute)
        steps.append(dict(symbol=symbol,attempt=1,minute=minute,credits=1))
    return dict(production_ready=False,required_lock_group='market-data-collector',
        steps=steps,projected_credits=core.strict_load(projected)['credits'],
        consumer_lineage_registration_required=INGEST_KIND)
