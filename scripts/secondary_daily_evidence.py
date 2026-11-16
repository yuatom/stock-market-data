"""Offline binding of retained bytes to a candidate; no transport or promotion.

Hashes establish local consistency, NOT origin authenticity or historical
availability. Yahoo actions/adjclose remain observations, never adjustment proof.
"""
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, localcontext
from urllib.parse import urlsplit, parse_qs
from zoneinfo import ZoneInfo
from secondary_daily_adapter import normalize_sina, normalize_yahoo, _number, _unique_object


def _clock(value):
    t = datetime.fromisoformat(value)
    if t.utcoffset() is None:
        raise ValueError('capture clock must include timezone')
    return t


def _event_day(timestamp, tz, cutoff):
    if type(timestamp) is not int:
        raise ValueError('event timestamp must be integer seconds')
    day = datetime.fromtimestamp(timestamp, tz).date()
    if day >= cutoff:
        raise ValueError('non-prior action date')
    return day.isoformat()


def _yahoo_observations(result, candidate, cutoff):
    tz = ZoneInfo(result['meta']['exchangeTimezoneName'])
    adjusted = result['indicators'].get('adjclose')
    candidate['adjusted_close_observations'] = None
    if adjusted is not None:
        if not isinstance(adjusted, list) or len(adjusted) != 1:
            raise ValueError('ambiguous adjusted close arrays')
        values = adjusted[0]['adjclose']
        if not isinstance(values, list) or len(values) != len(result['timestamp']):
            raise ValueError('adjusted close length mismatch')
        candidate['adjusted_close_observations'] = [
            {'trade_date':_event_day(t, tz, cutoff), 'value':_number(v, 'close')}
            for t, v in zip(result['timestamp'], values)]
    events = result.get('events')
    actions = {'status':'absent_not_proof_of_no_actions', 'splits':[], 'dividends':[], 'capitalGains':[]}
    if events is not None:
        if not isinstance(events, dict) or set(events) - {'splits','dividends','capitalGains'}:
            raise ValueError('unexpected corporate actions structure')
        actions['status'] = 'provided_coverage_unverified'
        for kind, items in events.items():
            if not isinstance(items, dict) or len(items) > 5000:
                raise ValueError('invalid or excessive actions')
            for event_id, event in items.items():
                if not isinstance(event, dict) or str(event.get('date')) != event_id:
                    raise ValueError('action id/date mismatch')
                row = {'source_event_id':event_id, 'trade_date':_event_day(event['date'],tz,cutoff)}
                if kind == 'splits':
                    n, d = _number(event['numerator'],'close'), _number(event['denominator'],'close')
                    ratio = event['splitRatio'].split(':')
                    if len(ratio) != 2:
                        raise ValueError('invalid split ratio')
                    rn, rd = _number(ratio[0],'close'), _number(ratio[1],'close')
                    with localcontext() as context:
                        context.prec = 256
                        if Decimal(n)*Decimal(rd) != Decimal(d)*Decimal(rn):
                            raise ValueError('conflicting split ratio')
                    row.update(numerator=n, denominator=d, ratio=event['splitRatio'])
                else:
                    row['amount'] = _number(event['amount'],'close')
                    if row['amount'] is None:
                        raise ValueError('missing action amount')
                actions[kind].append(row)
            actions[kind].sort(key=lambda x:x['trade_date'])
    candidate['corporate_actions'] = actions
    candidate['action_coverage_verified'] = False


def normalize_capture(raw, metadata, *, provider, symbol, as_of_date):
    """Validate retained-byte envelope, then normalize without I/O.

    Only known public endpoints/parameters accepted; caller must separately enforce
    report cutoff and preserve these bytes. This function grants no production use.
    """
    try:
        if not isinstance(raw, bytes) or not raw or len(raw) > 8_000_000:
            raise ValueError('invalid raw bytes or size')
        if metadata['transport'] != 'success' or type(metadata['http_status']) is not int or metadata['http_status'] != 200:
            raise ValueError('capture was not successful')
        digest = hashlib.sha256(raw).hexdigest()
        if type(metadata['bytes']) is not int or metadata['bytes'] != len(raw) or metadata['sha256'] != digest:
            raise ValueError('raw byte/hash mismatch')
        start, finish = _clock(metadata['started_at']), _clock(metadata['finished_at'])
        if finish < start:
            raise ValueError('reversed capture clock')
        url = metadata['url']; parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port or parsed.fragment or metadata['final_url'] != url:
            raise ValueError('unsafe or redirected request identity')
        q = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        if any(len(v)!=1 for v in q.values()):
            raise ValueError('ambiguous query parameters')
        if provider == 'yahoo':
            if parsed.netloc != 'query2.finance.yahoo.com' or parsed.path != '/v8/finance/chart/'+symbol or q.get('interval') != ['1d'] or set(q)-{'interval','range','events'}:
                raise ValueError('Yahoo request identity mismatch')
            payload = json.loads(raw.decode('utf-8'), parse_float=Decimal, object_pairs_hook=_unique_object)
            candidate = normalize_yahoo(payload,symbol=symbol,as_of_date=as_of_date)
            _yahoo_observations(payload['chart']['result'][0],candidate,date.fromisoformat(as_of_date))
        elif provider == 'sina':
            if parsed.netloc != 'stock.finance.sina.com.cn' or parsed.path != '/usstock/api/jsonp.php/var/US_MinKService.getDailyK' or q.get('symbol') != [symbol] or set(q)-{'symbol','num'}:
                raise ValueError('Sina request identity mismatch')
            candidate = normalize_sina(raw.decode('gbk'),symbol=symbol,as_of_date=as_of_date)
            candidate['corporate_actions'] = {'status':'not_provided_by_endpoint'}
            candidate['action_coverage_verified'] = False
        else:
            raise ValueError('unsupported provider')
        candidate['capture'] = {'sha256':digest,'bytes':len(raw),'url':url,'started_at':metadata['started_at'],
            'finished_at':metadata['finished_at'],'authenticated_origin':False,
            'historical_availability_proven':False,'binding':'local_bytes_and_declared_request'}
        return candidate
    except (KeyError, TypeError, AttributeError, IndexError, UnicodeError, OverflowError, OSError) as exc:
        raise ValueError('malformed capture or source observation') from exc
