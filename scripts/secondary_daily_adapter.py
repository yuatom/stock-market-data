"""Offline candidate daily-bar normalization, inspired by Vibe-Research endpoints.

No transport, credentials, Store writes, activation, retries or report routing.
Input must be a separately retained raw response. Never splice these candidates
into another provider's history. Adjustment/identity qualification is downstream.
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
import math
import re
import json
from zoneinfo import ZoneInfo

_FIELDS = ('open', 'high', 'low', 'close', 'volume')

def _number(value, field):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError('boolean numeric field')
    if field == 'volume':
        try:
            n = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError('invalid volume') from exc
        if not n.is_finite() or n < 0 or n != n.to_integral_value() or n > 2**63 - 1:
            raise ValueError('invalid integer volume')
        return int(n)
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('invalid numeric field') from exc
    if not n.is_finite() or n <= 0 or abs(n.adjusted()) > 100 or len(n.as_tuple().digits) > 64:
        raise ValueError('invalid numeric range or precision bound')
    # JSON-safe exact decimal text; never invent precision absent from input.
    return format(n, 'f')



def _result(provider, symbol, bars, as_of_date, identity):
    if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,19}', symbol):
        raise ValueError('invalid explicit symbol')
    cutoff = date.fromisoformat(as_of_date)
    if not bars or len(bars) > 5000:
        raise ValueError('empty or excessive daily response')
    seen = set()
    for bar in bars:
        d = date.fromisoformat(bar['trade_date'])
        if d >= cutoff or d in seen:
            raise ValueError('duplicate or non-prior-session date')
        seen.add(d)
        if bar['close'] is None:
            raise ValueError('missing close')
        values = [Decimal(bar[k]) for k in ('open','close') if bar[k] is not None]
        if bar['high'] is not None and any(v > Decimal(bar['high']) for v in values):
            raise ValueError('OHLC high conflict')
        if bar['low'] is not None and any(v < Decimal(bar['low']) for v in values):
            raise ValueError('OHLC low conflict')
        if bar['low'] is not None and bar['high'] is not None and Decimal(bar['low']) > Decimal(bar['high']):
            raise ValueError('OHLC range conflict')
    ordered = sorted(bars,key=lambda b:b['trade_date'])
    flags = []
    for previous, current in zip(ordered, ordered[1:]):
        # Diagnostic only, NOT split inference or proof of continuity below threshold.
        with localcontext() as context:
            context.prec = 128  # Inputs have <=64 digits; integer cross-products are exact.
            current5 = Decimal(current['close']) * 5
            previous3 = Decimal(previous['close']) * 3
            previous7 = Decimal(previous['close']) * 7
            discontinuous = current5 < previous3 or current5 > previous7
        if discontinuous:
            flags.append({'trade_date':current['trade_date'],
                          'reason':'large_price_discontinuity_unexplained'})
    return {'schema_version':'secondary-daily-candidate/v1',
            'provider':provider,'symbol':symbol,'session':'regular','interval':'1d',
            'market_fact_authority':False,'promotion_required':True,
            'identity_verification':identity,'adjustment_semantics':'unverified_provider_observation',
            'official_close_claim':False,'volume_eligible':False,
            'technical_eligible':False,'technical_block_reason':'adjustment unverified',
            'price_encoding':'decimal_string','quality_flags':flags,
            'bars':ordered}


def normalize_yahoo(payload, *, symbol, as_of_date):
    """Normalize chart-v8 candidate observations without rounding price values."""
    try:
        chart=payload['chart']
        if chart.get('error') or len(chart['result']) != 1:
            raise ValueError('Yahoo error or ambiguous result')
        result=chart['result'][0]; meta=result['meta']
        if meta['symbol'] != symbol or meta['dataGranularity'] != '1d' or meta['currency'] != 'USD':
            raise ValueError('Yahoo identity/interval/currency mismatch')
        tz=ZoneInfo(meta['exchangeTimezoneName'])
        times=result['timestamp']; quotes=result['indicators']['quote']
        if len(quotes)!=1 or not isinstance(times,list) or len(times)>5000:
            raise ValueError('ambiguous or excessive arrays')
        q=quotes[0]
        for field in _FIELDS:
            if field in q and (not isinstance(q[field],list) or len(q[field]) != len(times)):
                raise ValueError('array length mismatch')
        bars=[]
        for i,t in enumerate(times):
            if isinstance(t,bool) or not isinstance(t,(int,float)) or not math.isfinite(t):
                raise ValueError('invalid timestamp')
            bar={'trade_date':datetime.fromtimestamp(t,tz).date().isoformat()}
            bar.update({k:_number(q[k][i] if k in q else None,k) for k in _FIELDS})
            bars.append(bar)
        return _result('yahoo_chart_candidate',symbol,bars,as_of_date,'payload_symbol')
    except (KeyError,TypeError,IndexError,OverflowError,AttributeError) as exc:
        raise ValueError('malformed Yahoo payload') from exc


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('duplicate JSON member')
        obj[key] = value
    return obj


def normalize_sina(raw, *, symbol, as_of_date):
    """Accept a JSON array or strict var(...) wrapper, never execute JavaScript.

    This endpoint lacks response security identity: request-bound only. It may
    be compared offline but cannot automatically become an eligible Store fact.
    """
    if not isinstance(raw,str) or len(raw.encode('utf-8')) > 8_000_000:
        raise ValueError('invalid or oversized response')
    text=raw.strip()
    observed_prefix = "/*<script>location.href='//sina.com';</script>*/"
    if text.startswith(observed_prefix):
        text=text[len(observed_prefix):].lstrip()
        if not text.startswith('var('):
            raise ValueError('unexpected Sina wrapper after observed prefix')
    if text.startswith('var(') and text.endswith(');'):
        text=text[4:-2]
    try:
        rows=json.loads(text, parse_float=Decimal, object_pairs_hook=_unique_object)
        if not isinstance(rows,list) or len(rows)>5000:
            raise ValueError('invalid row array')
        bars=[]
        for row in rows:
            bar={'trade_date':row['d']}
            bar.update({k:_number(row.get(short),k) for k,short in zip(_FIELDS,('o','h','l','c','v'))})
            bars.append(bar)
        return _result('sina_daily_candidate',symbol,bars,as_of_date,'request_bound_only')
    except (KeyError,TypeError,AttributeError) as exc:
        raise ValueError('malformed Sina payload') from exc


def require_technical_eligibility(candidate):
    """Fail closed: this candidate module has no verified adjustment promotion path.

    Absence of a large jump is not evidence of split/dividend consistency.
    Callers cannot opt in by toggling metadata; promotion is a separate contract.
    """
    raise ValueError('adjustment unverified: candidate technical use is prohibited')
