"""Dynamic daily only: bounded raw transport plus pure strict parsing.
No capture publication, budget authority, provider provenance or billing claim.
"""
from decimal import Decimal
import math
import re
import urllib.parse
import market_data_collectors as collectors
import raw_response_evidence as evidence

FIELDS=('open','high','low','close','volume')


def fetch_raw(symbol,api_key,access,*,outputsize):
    spec=access['twelve_data_basic']
    evidence._require(spec['interval_daily']=='1day' and type(outputsize) is int and 1<=outputsize<=5000,'invalid dynamic daily request')
    params=urllib.parse.urlencode(dict(symbol=symbol,interval='1day',outputsize=outputsize,apikey=api_key,format='JSON'))
    return collectors._http_raw(f"{spec['base_url']}{spec['time_series_path']}?{params}",
        timeout=int(access['http']['timeout_seconds']),user_agent=str(access['http']['user_agent']),maximum_bytes=evidence.MAX_RAW_BYTES)


def parse(raw,*,symbol,outputsize):
    doc=evidence._load(raw,evidence.MAX_RAW_BYTES)
    evidence._require(type(doc) is dict and doc.get('status')=='ok','daily response not ok')
    meta=doc.get('meta')
    evidence._require(type(meta) is dict and meta.get('symbol')==symbol and meta.get('interval')=='1day','daily identity mismatch')
    rows=doc.get('values')
    evidence._require(type(rows) is list and 1<=len(rows)<=outputsize,'invalid daily row count')
    result=[];seen=set()
    for row in rows:
        evidence._closed(row,{'datetime',*FIELDS},'dynamic daily row')
        day=evidence._day(row['datetime']).isoformat()
        evidence._require(day not in seen,'duplicate daily date');seen.add(day)
        numbers={}
        for field in FIELDS:
            value=row[field]
            evidence._require(type(value) is str and 0<len(value)<=64 and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?',value),'decimal string required')
            number=Decimal(value)
            evidence._require(number.is_finite() and (number>=0 if field=='volume' else number>0),'invalid daily number')
            if field=='volume':evidence._require(number==number.to_integral_value(),'fractional volume rejected')
            numbers[field]=number
        evidence._require(numbers['low']<=min(numbers['open'],numbers['close'])<=max(numbers['open'],numbers['close'])<=numbers['high'],'invalid daily OHLC')
        converted={f:(int(numbers[f]) if f=='volume' else float(numbers[f])) for f in FIELDS}
        evidence._require(all(math.isfinite(converted[f]) and converted[f]>0 for f in FIELDS[:-1]),'numeric conversion rejected')
        result.append(dict(trade_date=day,**converted))
    return sorted(result,key=lambda x:x['trade_date'])
