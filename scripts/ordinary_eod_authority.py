"""Literal private Store evidence reader for dormant ordinary EOD.

An exact ref, immutable object graph, commitment and coherent scope do not
authenticate a real creator, credential, admission event or production grant.
The hosted registry remains empty. No fallback to finite or retained grants.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import NoReturn
from zoneinfo import ZoneInfo

from scripts.private_authority_input import STORE_REPOSITORY, _LocalGit

ROLE = "ordinary_previous_session_eod_authority_v1"
AUTHORITY_PATH = "ordinary-authority/mandate.json"
QUEUE = "market-data-collector"
CALLER = ".github/workflows/market-data-collector.yml"
ET = ZoneInfo("America/New_York")
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
SYMBOL = re.compile(r"[A-Z0-9]+(?:[.-][A-Z0-9]+)*")
STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


class OrdinaryAuthorityInvalid(RuntimeError):
    """A fixed, value-free failure."""

class OrdinaryEodUnregistered(RuntimeError):
    """A structurally accepted object cannot substitute native authority."""


def _need(condition: bool) -> None:
    if not condition:
        raise OrdinaryAuthorityInvalid("ORDINARY_EOD_AUTHORITY_INVALID")


def _matches(value: object, pattern: re.Pattern) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def canonical(obj) -> bytes:
    return (json.dumps(obj,sort_keys=True,separators=(",",":"),ensure_ascii=True,allow_nan=False)+"\n").encode("ascii")


def commitment(raw: bytes) -> str:
    return sha256(b"ordinary-previous-session-eod-authority:v1\0"+raw).hexdigest()


def scope_digest(scope: tuple[tuple[str,str], ...]) -> str:
    return sha256(b"ordinary-previous-session-eod-scope:v1\0"+canonical(
        [{"symbol":s,"asset_class":a} for s,a in scope]
    )).hexdigest()


def authority_ref(compute: str, digest: str) -> str:
    _need(_matches(compute,HEX40) and _matches(digest,HEX64))
    return f"refs/heads/ordinary-eod-authority/{compute}/{digest}"


@dataclass(frozen=True,repr=False)
class ExpectedOrdinaryScope:
    """Separately supplied deployment/market-calendar pins, not receipt claims."""
    compute_sha: str
    store_repository_id: int
    store_parent_sha: str
    commit_sha: str
    commitment_sha256: str
    execution_day: str
    previous_session: str
    scope: tuple[tuple[str,str], ...]
    valid_from: str
    valid_until: str


@dataclass(frozen=True,repr=False)
class PreparedOrdinaryEvidence:
    """Object identities only; never an execution authorization."""
    commit_sha: str
    tree_sha: str
    blob_sha: str
    commitment_sha256: str

    @property
    def authorization_authenticated(self) -> bool:
        return False

    @property
    def production_activation(self) -> bool:
        return False


def _clock_validate(e: ExpectedOrdinaryScope, now: datetime) -> None:
    _need(type(now) is datetime and now.tzinfo is not None and now.utcoffset() is not None)
    _need(_matches(e.valid_from,STAMP) and _matches(e.valid_until,STAMP))
    opening=datetime.fromisoformat(e.valid_from).astimezone(ET)
    cutoff=datetime.fromisoformat(e.valid_until).astimezone(ET)
    day=date.fromisoformat(e.execution_day)
    prior=date.fromisoformat(e.previous_session)
    _need(e.execution_day==day.isoformat() and e.previous_session==prior.isoformat() and prior < day)
    _need(opening.date()==cutoff.date()==now.astimezone(ET).date()==day)
    _need(opening.strftime("%H:%M")>="07:00" and opening<=now<cutoff)


def _bind_selection(reader: _LocalGit, e: ExpectedOrdinaryScope) -> None:
    ref=authority_ref(e.compute_sha,e.commitment_sha256)
    fmt="--format=%(refname) %(objectname) %(objecttype) %(symref)"
    _need(reader.read("for-each-ref",fmt,ref)==f"{ref} {e.commit_sha} commit \n".encode("ascii"))
    main="refs/heads/main"
    _need(reader.read("for-each-ref",fmt,main)==f"{main} {e.store_parent_sha} commit \n".encode("ascii"))
    _need(reader.text("rev-parse","--verify","HEAD")==e.store_parent_sha)


def _bind_bytes(raw: bytes, e: ExpectedOrdinaryScope) -> None:
    value=json.loads(raw)
    _need(type(value) is dict and canonical(value)==raw)
    size=len(e.scope)
    exact_values={"kind":ROLE,"compute_sha":e.compute_sha,"workflow_source_sha":e.compute_sha,
        "store_parent_sha":e.store_parent_sha,"execution_day":e.execution_day,"previous_session":e.previous_session,
        "market":"XNYS","provider":"twelve_data_basic","queue":QUEUE,"workflow":CALLER,
        "trigger_policy":"native_schedule_or_authorized_manual_first_attempt",
        "identity_scope":[{"symbol":s,"asset_class":a} for s,a in e.scope],
        "scope_sha256":scope_digest(e.scope),"valid_from":e.valid_from,"valid_until":e.valid_until}
    exact_ints={"schema_version":1,"store_repository_id":e.store_repository_id,"max_attempts":1,
                "max_requests":size,"max_credits":size}
    _need(set(value)==set(exact_values)|set(exact_ints)|{"allow_paid_upgrade","nonce"})
    _need(all(value[k]==v for k,v in exact_values.items()))
    _need(all(type(value[k]) is int and value[k]==v for k,v in exact_ints.items()))
    _need(value["allow_paid_upgrade"] is False and _matches(value["nonce"],HEX64)
          and value["nonce"]!="0"*64)
    _need(commitment(raw)==e.commitment_sha256)


def read_ordinary_authority(repo: Path, expected: ExpectedOrdinaryScope, *,
                            now: datetime) -> PreparedOrdinaryEvidence:
    """Read exact locally materialized native objects. No network or effects."""
    try:
        e=expected
        _need(type(e) is ExpectedOrdinaryScope and all(_matches(x,HEX40) for x in (
            e.compute_sha,e.store_parent_sha,e.commit_sha)) and _matches(e.commitment_sha256,HEX64))
        _need(type(e.store_repository_id) is int and e.store_repository_id>0)
        _need(type(e.scope) is tuple and 1<=len(e.scope)<=200
              and all(type(x) is tuple and len(x)==2 and _matches(x[0],SYMBOL) and len(x[0])<=20
                      and x[1] in ("stocks","etf") for x in e.scope)
              and tuple(sorted(e.scope))==e.scope and len(dict(e.scope))==len(e.scope))
        _clock_validate(e,now)
        r=_LocalGit(repo)
        # This is only a local routing guard, not authenticated server identity.
        root=f"https://github.com/{STORE_REPOSITORY}"
        _need(r.text("remote","get-url","origin") in (root,root+".git"))
        _bind_selection(r,e)
        tree,_=r.commit(e.commit_sha)
        blob,raw=r.blob(e.commit_sha,AUTHORITY_PATH)
        r.single_addition(e.commit_sha,e.store_parent_sha,AUTHORITY_PATH,blob)
        _bind_bytes(raw,e)
        r.assert_unmodified_graph()
        _bind_selection(r,e)
        return PreparedOrdinaryEvidence(e.commit_sha,tree,blob,e.commitment_sha256)
    except Exception:
        pass
    raise OrdinaryAuthorityInvalid("ORDINARY_EOD_AUTHORITY_INVALID")


def require_hosted_binding(_prepared: PreparedOrdinaryEvidence | None = None) -> NoReturn:
    """Authoritative admission boundary, deliberately unregistered.

    The future connector must independently authenticate the exact private
    Store, reviewed deployment and native event/queue; demonstrate the actual
    creator/principal of the immutable object and its authorization separately
    from the content writer/runner; and deliver a native claim owner. No header,
    body field, ambient variable, Git author, token-shaped string or caller
    flag can provide this binding, and the finite route cannot stand in.
    """
    raise OrdinaryEodUnregistered("ORDINARY_EOD_VERIFIED_CREATOR_UNREGISTERED")
