"""Explicit dynamic-only paid fallback. Original shared fixed transport unchanged."""
import hashlib,json,os,urllib.parse
import market_data_collectors as base
import admit_market_data_baseline as admission
from raw_response_evidence import _load,EvidenceError

class KnownIntradayRejection(RuntimeError):pass

def fetch_guarded(symbol,asset_class,trade_date,start_et,end_et,*,store_root,config,access,writer):
    spec=access['twelve_data_basic'];key=os.environ.get(str(spec.get('api_key_env') or 'TWELVE_DATA_API_KEY'))
    if not key:raise KnownIntradayRejection('secret_missing')
    if not writer.intraday_scope or writer.intraday_scope['interval']!=str(spec.get('interval_intraday') or '1min'):writer.abort()
    params=urllib.parse.urlencode(dict(symbol=symbol,interval='1min',start_date=f'{trade_date} {start_et}:00',end_date=f'{trade_date} {end_et}:00',timezone=str(spec.get('intraday_timezone') or 'America/New_York'),prepost='false',order='asc',outputsize=int(spec.get('intraday_outputsize') or 90),format='JSON',apikey=key))
    writer.reserve(symbol,admission.load_helpers(),config['collector']['budgets']['twelve_data_basic'])
    try:
        raw=base._http_raw(f"{spec['base_url']}{spec['time_series_path']}?{params}",timeout=int(access['http']['timeout_seconds']),user_agent=str(access['http']['user_agent']),maximum_bytes=4*1024*1024,before_request=writer.before_request)
        relative=writer.raw_prefix(symbol)+hashlib.sha256(raw).hexdigest()+'.raw'
        target='data/market-data/'+relative;writer.allowed.add(target)
        path=writer.safe_path(target);path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists():
            if path.read_bytes()!=raw:writer.abort()
        else:path.write_bytes(raw)
        writer.retain_raw(relative)
    except Exception:writer.abort()
    try:
        payload=_load(raw,4*1024*1024)
        if not isinstance(payload,dict):raise KnownIntradayRejection('response_shape_rejected')
        if payload.get('status')=='error':raise KnownIntradayRejection('provider_rejected')
        meta=payload.get('meta')
        if not isinstance(meta,dict) or meta.get('symbol')!=symbol or meta.get('interval')!='1min':raise KnownIntradayRejection('provider_metadata_rejected')
        if not isinstance(payload.get('values'),list):raise KnownIntradayRejection('response_values_rejected')
        from market_data_collection import parse_twelve_regular
        return parse_twelve_regular(payload,symbol,asset_class,trade_date,start_et,end_et)
    except (EvidenceError,KnownIntradayRejection):raise KnownIntradayRejection('retained_response_rejected') from None
    except Exception:writer.abort()
