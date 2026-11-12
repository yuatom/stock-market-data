"""Immutable provider-vintage candidates and explicit read-only price admission.

No HTTP, publisher, activation writer, grant creation or legacy rescaling exists
here. A receipt commitment is byte binding, never proof of user authorization.
The trusted runtime must obtain the expected commitment and source pin through
the separately approved private authority channel, not from a research draft.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
import hashlib
import hmac
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo
import re

from scripts.raw_response_evidence import verify_envelope, validate_request
from scripts.private_authority_input import _LocalGit

PRICE_FIELDS = ('open', 'high', 'low', 'close')
PROVIDER = 'twelve_data_basic'
PREFIX = 'data/market-data/'
DOMAIN = b'basis-consumer-receipt:v1\x00'
MAX_BYTES = 12 * 1024 * 1024


class BasisError(RuntimeError):
    """Value-free errors: no private pins, prices or payloads in diagnostics."""


def _need(condition):
    if not condition:
        raise BasisError('BASIS_CONTRACT_REJECTED')


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                       allow_nan=False)+'\n').encode('ascii')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def blob_sha(raw):
    return hashlib.sha1(f'blob {len(raw)}\0'.encode()+raw).hexdigest()


def _hex(value, length=40):
    _need(type(value) is str and re.fullmatch('[0-9a-f]{'+str(length)+'}', value))
    return value


def _day(value):
    _need(type(value) is str and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value))
    _need(date.fromisoformat(value).isoformat() == value)
    return value


def _stamp(value):
    _need(type(value) is str and len(value) <= 40)
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    _need(stamp.tzinfo is not None)
    return stamp


def _closed(value, fields):
    _need(type(value) is dict and set(value) == set(fields))


def _parse(raw, *, canonical=True, decimal=False):
    _need(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES)
    def pairs(items):
        result = {}
        for k, v in items:
            _need(k not in result)
            result[k] = v
        return result
    def constant(_):
        raise BasisError('BASIS_CONTRACT_REJECTED')
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant,
                       parse_float=Decimal if decimal else float)
    if canonical:
        _need(encode(value) == raw)
    return value


def _identity(provider, symbol):
    _need(provider == PROVIDER and type(symbol) is str and re.fullmatch(r'[A-Z][A-Z0-9.-]{0,14}', symbol))


def catalog_path(provider, symbol):
    _identity(provider, symbol)
    return f'{PREFIX}series-active/daily/{provider}/{symbol}.json'


def snapshot_path(provider, symbol, basis_id):
    _identity(provider, symbol); _hex(basis_id, 64)
    return f'{PREFIX}series-versions/daily/{provider}/{symbol}/{basis_id}/snapshot.json'


def admission_path(provider, symbol, admission_blob_sha):
    _identity(provider, symbol); _hex(admission_blob_sha)
    return f'{PREFIX}series-admissions/daily/{provider}/{symbol}/{admission_blob_sha}.json'


def _numbers(row):
    numbers = []
    for key in (*PRICE_FIELDS, 'volume'):
        value = row[key]
        _need(type(value) is str and 0 < len(value) <= 64 and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value))
        numbers.append(Decimal(value))
    op, hi, lo, cl, volume = numbers
    _need(0 < lo <= min(op, cl) <= max(op, cl) <= hi and volume >= 0)
    _need(volume == volume.to_integral_value())
    return numbers


def prepare_snapshot(envelope_raw, expected_envelope_sha256, baseline_raw, split_event):
    """Pure candidate creation. Baseline input is diagnostic, not original-blob proof."""
    try:
        v = verify_envelope(envelope_raw, expected_envelope_sha256)
        old = _parse(baseline_raw, canonical=False, decimal=True)
        if type(old) is dict:
            _closed(old, ('source_commit', 'kind', 'records'))
            old = old['records']
        _need(type(old) is list and 0 < len(old) <= 5000)
        event = json.loads(encode(split_event))
        _closed(event, ('kind', 'numerator', 'denominator', 'effective_date', 'source_url'))
        _need(event['kind'] == 'forward_split' and type(event['numerator']) is int and
              type(event['denominator']) is int and 0 < event['denominator'] < event['numerator'] <= 1000)
        effective = _day(event['effective_date'])
        _need(type(event['source_url']) is str and event['source_url'].startswith('https://'))
        old_dates = [_day(r['trade_date']) for r in old]
        _need(old_dates == sorted(set(old_dates)) and old_dates[-1] < effective)
        rows = v['records']; by_date = {r['datetime']: r for r in rows}
        _need(set(old_dates) <= set(by_date) and rows[0]['datetime'] == old_dates[0])
        for r in rows:
            _numbers(r)
        differences = []
        numerator, denominator = event['numerator'], event['denominator']
        for r in old:
            _need(type(r) is dict and set(r) <= {'trade_date', *PRICE_FIELDS, 'volume', 'source_timestamp'})
            for f in (*PRICE_FIELDS, 'volume'):
                _need(f in r and type(r[f]) in (int, Decimal) and not isinstance(r[f], bool))
                _need(Decimal(r[f]).is_finite() and Decimal(r[f]) >= 0)
            reported = by_date[r['trade_date']]['volume']
            # Comparison only; neither result is substituted for a stored value.
            residual = Fraction(reported) - Fraction(r['volume'])*Fraction(numerator, denominator)
            if residual:
                differences.append(dict(trade_date=r['trade_date'],old_volume=str(r['volume']),
                    provider_volume=reported,difference_from_expected=str(residual),
                    classification='UNRESOLVED_PROVIDER_VINTAGE_DIFFERENCE'))
        body = dict(schema_version=2,kind='literal_provider_basis_snapshot',provider=PROVIDER,
            symbol=v['request']['symbol'],request=v['request'],observed_at=v['observed_at'],
            envelope_sha256=sha(envelope_raw),raw_response_sha256=sha(v['raw_response']),
            baseline_evidence_sha256=sha(baseline_raw),split_event=event,
            basis_semantics='provider_reported_splits_snapshot',price_equivalence='NOT_ASSERTED',
            market_qualified=False,production_activation=False,volume_policy='BLOCKED_UNRESOLVED',
            unresolved_volume_dates=[x['trade_date'] for x in differences],volume_differences=differences,
            first_trade_date=rows[0]['datetime'],last_trade_date=rows[-1]['datetime'],
            record_count=len(rows),records=rows)
        return body | {'basis_id': sha(encode(body))}
    except Exception:
        pass
    raise BasisError('BASIS_CANDIDATE_REJECTED')


def _validate_snapshot(snapshot, expected_id):
    _need(type(snapshot) is dict and snapshot.get('basis_id') == _hex(expected_id, 64))
    _closed(snapshot, ('schema_version','kind','provider','symbol','request','observed_at',
        'envelope_sha256','raw_response_sha256','baseline_evidence_sha256','split_event',
        'basis_semantics','price_equivalence','market_qualified','production_activation',
        'volume_policy','unresolved_volume_dates','volume_differences','first_trade_date',
        'last_trade_date','record_count','records','basis_id'))
    body = {k: v for k, v in snapshot.items() if k != 'basis_id'}
    _need(sha(encode(body)) == expected_id)
    request = validate_request(snapshot['request'])
    _need(request['provider'] == 'twelve_data' and request['symbol'] == snapshot['symbol'] and
          request['interval'] == '1day' and request['adjust'] == 'splits' and
          snapshot['provider'] == PROVIDER and
          snapshot['basis_semantics'] == 'provider_reported_splits_snapshot')
    for key in ('envelope_sha256','raw_response_sha256','baseline_evidence_sha256'):
        _hex(snapshot[key],64)
    _stamp(snapshot['observed_at'])
    event = snapshot['split_event']
    _closed(event, ('kind','numerator','denominator','effective_date','source_url'))
    _need(event['kind'] == 'forward_split' and type(event['numerator']) is int and
          type(event['denominator']) is int and 0 < event['denominator'] < event['numerator'] <= 1000)
    _day(event['effective_date'])
    _need(type(event['source_url']) is str and event['source_url'].startswith('https://'))
    _need(snapshot.get('schema_version') == 2 and snapshot.get('kind') == 'literal_provider_basis_snapshot')
    _need(snapshot.get('market_qualified') is False and snapshot.get('production_activation') is False)
    _need(snapshot.get('price_equivalence') == 'NOT_ASSERTED' and snapshot.get('volume_policy') == 'BLOCKED_UNRESOLVED')
    _identity(snapshot['provider'], snapshot['symbol'])
    rows = snapshot['records']; _need(type(rows) is list and 0 < len(rows) <= 5000)
    dates = []
    for row in rows:
        _closed(row, ('datetime', *PRICE_FIELDS, 'volume')); _numbers(row); dates.append(_day(row['datetime']))
    _need(dates == sorted(set(dates)) and type(snapshot['record_count']) is int and snapshot['record_count'] == len(rows))
    _need(snapshot['first_trade_date'] == dates[0] and snapshot['last_trade_date'] == dates[-1])
    _need(request['start_date'] <= dates[0] <= dates[-1] <= request['end_date'])
    exceptions = snapshot['unresolved_volume_dates']
    _need(type(exceptions) is list and exceptions == sorted(set(exceptions)) and set(exceptions) <= set(dates))
    differences = snapshot['volume_differences']
    _need(type(differences) is list and [x['trade_date'] for x in differences] == exceptions)
    for difference in differences:
        _closed(difference, ('trade_date','old_volume','provider_volume','difference_from_expected','classification'))
        _need(difference['classification'] == 'UNRESOLVED_PROVIDER_VINTAGE_DIFFERENCE')
        _need(difference['provider_volume'] == rows[dates.index(difference['trade_date'])]['volume'])
        old_volume = Fraction(difference['old_volume']); residual = Fraction(difference['difference_from_expected'])
        _need(old_volume >= 0 and residual != 0)
        _need(Fraction(difference['provider_volume']) - old_volume*Fraction(event['numerator'],event['denominator']) == residual)
    return snapshot


def stage_snapshot(snapshot, destination):
    """Create a private local artifact, never a Store activation or publication."""
    try:
        _validate_snapshot(snapshot, snapshot['basis_id'])
        target = Path(destination)
        _need(target.is_absolute() and not any(p.is_symlink() for p in (target, *target.parents)))
        target.mkdir(mode=0o700, exist_ok=False)
        raw = encode(snapshot)
        manifest = encode(dict(schema_version=1,status='LOCAL_CANDIDATE_ONLY',basis_id=snapshot['basis_id'],
             snapshot_sha256=sha(raw),market_qualified=False,production_activation=False))
        for name, payload in (('snapshot.json', raw), ('manifest.json', manifest)):
            fd = os.open(target/name, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(payload); f.flush(); os.fsync(f.fileno())
            _need((target/name).read_bytes() == payload)
        verify_staged_snapshot(target, snapshot['basis_id'])
    except Exception:
        pass
    else:
        return
    raise BasisError('BASIS_STAGING_REJECTED')


def verify_staged_snapshot(destination, expected_id):
    try:
        target = Path(destination)
        _need(target.is_absolute() and not any(p.is_symlink() for p in (target, *target.parents)))
        _need({p.name for p in target.iterdir()} == {'snapshot.json', 'manifest.json'})
        for name in ('snapshot.json', 'manifest.json'):
            f = target/name; _need(f.is_file() and not f.is_symlink() and f.stat().st_nlink == 1)
        raw = (target/'snapshot.json').read_bytes(); manifest = _parse((target/'manifest.json').read_bytes())
        _need(manifest == dict(schema_version=1,status='LOCAL_CANDIDATE_ONLY',basis_id=expected_id,
             snapshot_sha256=sha(raw),market_qualified=False,production_activation=False))
        return _validate_snapshot(_parse(raw), expected_id)
    except Exception:
        pass
    raise BasisError('BASIS_STAGING_REJECTED')


@dataclass(frozen=True, repr=False)
class ReadAuthority:
    """Private trusted-runtime dependency. Never deserialize this from a draft."""
    selector_sha256: str
    admission_blob_sha: str

    def __repr__(self):
        return '<ReadAuthority>'


def _selection(selector):
    _closed(selector, ('schema_version','provider','symbol','basis_id','snapshot_blob_sha','admission_blob_sha','read_sha'))
    _need(type(selector['schema_version']) is int and selector['schema_version'] == 1)
    _identity(selector['provider'],selector['symbol']); _hex(selector['basis_id'],64)
    for k in ('snapshot_blob_sha','admission_blob_sha','read_sha'):
        _hex(selector[k])


def _store(repo):
    g = _LocalGit(repo)
    _need(g.text('remote','get-url','origin') in (
        'https://github.com/yuatom/stock-market-data-store',
        'https://github.com/yuatom/stock-market-data-store.git',
        'git@github.com:yuatom/stock-market-data-store.git'))
    return g


def _blob(g, commit, relative, expected=None):
    oid = g.normal_blob_identity(commit,relative)
    if expected is not None:
        _need(oid == _hex(expected))
    return g.object(oid,'blob',MAX_BYTES)


def read_price_view(repo, selector, authority, *, fields, required_sessions, knowledge_cutoff):
    """Read immutable committed bytes only. All request context must be explicit."""
    try:
        _selection(selector)
        _need(type(authority) is ReadAuthority)
        _need(hmac.compare_digest(_hex(authority.selector_sha256,64),sha(encode(selector))) and
              authority.admission_blob_sha == selector['admission_blob_sha'])
        _need(type(fields) in (tuple,list) and fields and len(fields) == len(set(fields)) and set(fields) <= set(PRICE_FIELDS))
        _need(type(required_sessions) in (tuple,list) and required_sessions)
        dates = [_day(x) for x in required_sessions]
        _need(dates == sorted(set(dates)))
        cutoff = _stamp(knowledge_cutoff)
        g = _store(repo); commit = selector['read_sha']; provider=selector['provider']; symbol=selector['symbol']
        catalog = _parse(_blob(g,commit,catalog_path(provider,symbol)))
        _need(catalog == {k:v for k,v in selector.items() if k != 'read_sha'})
        snapshot = _validate_snapshot(_parse(_blob(g,commit,snapshot_path(provider,symbol,selector['basis_id']),
                                                    selector['snapshot_blob_sha'])),selector['basis_id'])
        _need(snapshot['provider'] == provider and snapshot['symbol'] == symbol)
        a = _parse(_blob(g,commit,admission_path(provider,symbol,selector['admission_blob_sha']),selector['admission_blob_sha']))
        _closed(a, ('schema_version','kind','status','basis_id','allowed_fields','volume_policy','admitted_at',
                    'proof_model','envelope_ref','workflow_evidence','unresolved_volume_dates'))
        _need(type(a['schema_version']) is int and a['schema_version'] == 1 and
              a['kind'] == 'provider_snapshot_price_admission' and a['status'] == 'ADMITTED_PRICE_ONLY' and
              a['basis_id'] == selector['basis_id'] and a['allowed_fields'] == list(PRICE_FIELDS) and
              a['volume_policy'] == 'BLOCKED_UNRESOLVED' and
              a['proof_model'] == 'reviewed_workflow_receipt_not_provider_signature' and
              a['unresolved_volume_dates'] == snapshot['unresolved_volume_dates'])
        _need(_stamp(snapshot['observed_at']) <= _stamp(a['admitted_at']) <= cutoff)
        ref = a['envelope_ref']; _closed(ref, ('path','blob_sha'))
        _need(ref['path'].startswith('capture-evidence/'))
        e = verify_envelope(_blob(g,commit,ref['path'],ref['blob_sha']),snapshot['envelope_sha256'])
        _need(e['records'] == snapshot['records'] and e['request'] == snapshot['request'] and
              e['observed_at'] == snapshot['observed_at'] and sha(e['raw_response']) == snapshot['raw_response_sha256'])
        w = a['workflow_evidence']; _closed(w, ('compute_sha','request_id','terminal_ref'))
        _hex(w['compute_sha']); _need(type(w['request_id']) is str and re.fullmatch(r'[1-9][0-9]*-1',w['request_id']))
        tref=w['terminal_ref']; _closed(tref, ('path','blob_sha')); _need(tref['path'].startswith('capture-evidence/'))
        terminal=_parse(_blob(g,commit,tref['path'],tref['blob_sha']))
        _need(all(terminal.get(k) == v for k,v in dict(status='CAPTURE_RETAINED_UNQUALIFIED',
            compute_sha=w['compute_sha'],request_id=w['request_id'],envelope_sha256=snapshot['envelope_sha256'],
            raw_response_sha256=snapshot['raw_response_sha256']).items()))
        by_date = {r['datetime']:r for r in snapshot['records']}
        # Expected sessions come from the consumer's separately verified calendar,
        # never inferred from provider output or silently shortened to available rows.
        _need(all(d in by_date for d in dates))
        _need([d for d in by_date if dates[0] <= d <= dates[-1]] == dates)
        _need(dates[-1] <= cutoff.astimezone(ZoneInfo('America/New_York')).date().isoformat())
        out=[{'trade_date':d,**{f:by_date[d][f] for f in fields}} for d in dates]
        return dict(schema_version=1,kind='explicit_price_only_view',basis_id=selector['basis_id'],
            read_sha=commit,record_count=len(out),first_trade_date=dates[0],last_trade_date=dates[-1],
            records=out,volume_status='BLOCKED_UNRESOLVED',source_authentication='NOT_PROVIDER_SIGNATURE')
    except Exception:
        pass
    raise BasisError('BASIS_READ_REJECTED')


def _receipt(raw):
    r = _parse(raw)
    _closed(r, ('schema_version','kind','compute_sha','selector','admission_blob_sha','nonce','valid_from','valid_until'))
    _need(type(r['schema_version']) is int and r['schema_version'] == 1 and r['kind'] == 'basis_consumer_receipt')
    _hex(r['compute_sha']); _hex(r['admission_blob_sha']); _hex(r['nonce'],64)
    _need(r['nonce'] != '0'*64 and _stamp(r['valid_from']) < _stamp(r['valid_until']))
    _selection(r['selector']); _need(r['selector']['admission_blob_sha'] == r['admission_blob_sha'])
    return r


def receipt_commitment(raw):
    """Same salted private-receipt pattern; caller supplies fresh random nonce."""
    try:
        _receipt(raw)
        return sha(DOMAIN+raw)
    except Exception:
        pass
    raise BasisError('BASIS_RECEIPT_REJECTED')


def verify_consumer_receipt(raw, expected_commitment, expected_compute_sha, *, now):
    """Bindings, not consent. Expectations must come from approved runtime inputs."""
    try:
        _hex(expected_commitment,64); _hex(expected_compute_sha)
        r = _receipt(raw)
        _need(hmac.compare_digest(sha(DOMAIN+raw),expected_commitment) and r['compute_sha'] == expected_compute_sha)
        _need(_stamp(r['valid_from']) <= _stamp(now) < _stamp(r['valid_until']))
        return r['selector'], ReadAuthority(sha(encode(r['selector'])),r['admission_blob_sha'])
    except Exception:
        pass
    raise BasisError('BASIS_RECEIPT_REJECTED')


def load_consumer_receipt(repo, expected_commitment, expected_compute_sha, *, now):
    """Read already-materialized private receipt ref. No fetch or credentials.

The future hosted adapter must privately fetch this exact ref with existing Store
credentials, prove repository privacy and explicit approval, then invoke here.
Only reviewed source SHA and approved opaque commitment may be public inputs.
"""
    try:
        _hex(expected_commitment,64); _hex(expected_compute_sha)
        g = _store(repo)
        ref = f'refs/heads/basis-read-receipts/{expected_compute_sha}/{expected_commitment}'
        record = g.read('for-each-ref','--format=%(refname) %(objectname) %(objecttype) %(symref)',ref)
        match = re.fullmatch(re.escape(ref.encode())+rb' ([0-9a-f]{40}) commit \n',record)
        _need(match)
        commit = match.group(1).decode()
        raw = _blob(g,commit,'basis-authority/receipt.json')
        return verify_consumer_receipt(raw,expected_commitment,expected_compute_sha,now=now)
    except Exception:
        pass
    raise BasisError('BASIS_RECEIPT_REJECTED')
