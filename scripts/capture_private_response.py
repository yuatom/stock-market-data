"""Opt-in local capture boundary; no workflow, approval source, debit or publisher.

The official caller must authenticate the scope, retain an independent scope/hash
anchor, hold the existing global collector lock and obtain a fresh durable debit.
This module binds those supplied values, but cannot authenticate user permission
or global serialization. It never turns a caller-supplied scope into authority.
"""
import hashlib
import json
import math
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

import admit_market_data_baseline as admission
from private_capture_retention import PrivateCaptureRetention
from raw_response_evidence import validate_request
from rebase_market_data_baseline import capture_daily_response


SCOPE_KEYS=set('schema_version mode request destination request_id compute_sha execution_day valid_from valid_until request_limit credit_limit debit_commit_sha budget_blob_sha budget_minute'.split())


class CaptureBoundaryError(RuntimeError):
    """Value-free scope/local boundary failure, with no implicit retry."""


def _step(action):
    try:
        return action()
    except Exception:
        pass
    raise CaptureBoundaryError('capture_private_preflight_failed')


def _require(condition):
    if not condition:
        raise ValueError('capture scope invalid')


def _encoded(value):
    return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)+'\n').encode()


def _stamp(value):
    _require(type(value) is str and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)',value))
    return datetime.fromisoformat(value)


def capture_with_private_retention(*,scope,request,destination,writer,debit,compute,
                                  compute_sha,execution_day,deadline,api_key,access,clock=None):
    """One explicitly invoked capture with local create-only evidence retention.

No new debit, global lock, permission or remote persistence is created. A local
destination collision/partial write is fatal. The exact existing provider guard
must succeed; its current-source and durable-budget checks remain unchanged.
"""
    clock=clock or (lambda:datetime.now(timezone.utc))
    def prepare():
        _require(type(scope) is dict and set(scope)==SCOPE_KEYS)
        frozen=json.loads(_encoded(scope))
        _require(type(frozen['schema_version']) is int and frozen['schema_version']==1 and frozen['mode']=='capture_only')
        for key in ('request_limit','credit_limit'):
            _require(type(frozen[key]) is int and frozen[key]==1)
        validated=validate_request(request)
        _require(validated==validate_request(frozen['request']))
        target=Path(destination)
        _require(target.is_absolute() and '..' not in target.parts and str(target)==frozen['destination'])
        _require(type(frozen['request_id']) is str and re.fullmatch(r'[0-9]+-[0-9]+',frozen['request_id']) and
                 frozen['request_id']==writer.request_id)
        for key in ('compute_sha','debit_commit_sha','budget_blob_sha'):
            _require(type(frozen[key]) is str and re.fullmatch(r'[0-9a-f]{40}',frozen[key]))
        _require(frozen['compute_sha']==compute_sha and frozen['debit_commit_sha']==debit.store_commit_sha and
                 frozen['budget_blob_sha']==debit.budget_blob_sha and frozen['budget_minute']==debit.minute)
        _require(type(frozen['budget_minute']) is str and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}Z',frozen['budget_minute']))
        _require(type(execution_day) is str and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',execution_day))
        _require(frozen['execution_day']==execution_day==writer.day and date.fromisoformat(validated['end_date'])<=date.fromisoformat(execution_day))
        start,end=_stamp(frozen['valid_from']),_stamp(frozen['valid_until'])
        _require(start<end and type(deadline) in (int,float) and math.isfinite(deadline) and time.monotonic()<deadline)
        def check_time():
            now=clock()
            _require(isinstance(now,datetime) and now.utcoffset() is not None and start<=now<end)
            _require(now.astimezone(admission.ET).date().isoformat()==execution_day)
            return now
        now=check_time()
        effective_deadline=min(deadline,time.monotonic()+(end-now).total_seconds())
        return frozen,validated,target,check_time,effective_deadline
    frozen,validated,target,check_time,effective_deadline=_step(prepare)
    scope_sha=hashlib.sha256(_encoded(frozen)).hexdigest()
    sink=_step(lambda:PrivateCaptureRetention(target,validated,excluded_roots=(compute,writer.repo)))
    with sink:
        def before_request():
            check_time()
            sink.claim_attempt(scope_sha256=scope_sha)
            admission.guard_provider_request(writer,debit,Path(compute),compute_sha,execution_day,
                clock=clock,deadline=effective_deadline)
            # The existing guard checks its clocks after remote reads. Recheck
            # the capture-specific expiry as well, without further filesystem I/O.
            check_time()
        result=capture_daily_response(request=validated,api_key=api_key,access=access,
            before_request=before_request,retain_raw=sink.retain_raw,
            retain_envelope=sink.retain_envelope,clock=clock)
    return dict(result,scope_sha256=scope_sha,local_files_fsynced_and_read_back=True,
                authorization_authenticated=False,global_lock_authenticated=False)
