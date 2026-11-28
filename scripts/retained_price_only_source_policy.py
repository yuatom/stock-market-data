"""Release-pinned eligibility policy; approval signatures cannot expand it."""
import re
from urllib.parse import urlsplit
from scripts import retained_price_only_slots as slots

POLICY_PATH='config/retained-price-only-source-eligibility.json'
LICENSED='licensed_regular_daily_price_export_v1'
WEB={
    'stockanalysis_spglobal_historical_html':('stockanalysis_spglobal','https://stockanalysis.com'),
    'barchart_raymond_james_historical_html':('barchart_websol','https://raymondjames.websol.barchart.com'),
}
NAME=re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
DIGEST=re.compile(r'[0-9a-f]{64}\Z')
CORROBORATION_DOMAINS=('stockanalysis.com','barchart.com')
class PolicyHold(ValueError):pass

def require(value):
    if not value:raise PolicyHold('RETAINED_PRICE_SOURCE_NOT_ELIGIBLE')

def _web(url):
    require(type(url) is str and 0<len(url)<=500)
    u=urlsplit(url)
    require(u.scheme=='https' and u.username is None and u.password is None and u.port is None
            and u.hostname is not None and u.netloc==u.hostname and
            re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)+',u.hostname) and not u.fragment)
    return u

def load_policy(raw):
    try:
        require(type(raw) is bytes and 0<len(raw)<=16000)
        document=slots.parse(raw)
        require(type(document) is dict and set(document)=={'schema_version','kind','profiles'} and
                type(document['schema_version']) is int and document['schema_version']==1 and
                document['kind']=='retained_price_only_source_eligibility' and
                type(document['profiles']) is list and 2<=len(document['profiles'])<=10)
        out={}
        for p in document['profiles']:
            require(type(p) is dict and set(p)=={'source_id','provider_identity','role','adapter','origin','license_sha256','valid_from','valid_until'})
            source=p['source_id'];provider=p['provider_identity']
            require(type(source) is str and NAME.fullmatch(source) and source not in out and
                    type(provider) is str and NAME.fullmatch(provider))
            u=_web(p['origin']);require(p['origin']==f'https://{u.hostname}')
            if source in WEB:
                require((provider,p['origin'])==WEB[source] and p['role']=='corroboration_only' and
                        p['adapter']=='offline_public_web_crosscheck' and
                        p['license_sha256'] is None and p['valid_from'] is None and p['valid_until'] is None)
            else:
                require(not any(u.hostname==domain or u.hostname.endswith('.'+domain) for domain in CORROBORATION_DOMAINS) and
                        provider not in {x[0] for x in WEB.values()} and
                        p['role']=='admission_eligible' and p['adapter']==LICENSED and
                        type(p['license_sha256']) is str and DIGEST.fullmatch(p['license_sha256'])
                        and p['license_sha256']!='0'*64 and slots.stamp(p['valid_from'])<slots.stamp(p['valid_until']))
            out[source]=p
        require(set(WEB)<=set(out));return out
    except Exception:
        raise PolicyHold('RETAINED_PRICE_SOURCE_NOT_ELIGIBLE') from None

def select(policy,source_urls,*,now):
    try:
        require(type(source_urls) is dict and len(source_urls)==2)
        chosen={}
        for name,url in source_urls.items():
            p=policy[name];u=_web(url)
            require(p['role']=='admission_eligible' and p['adapter']==LICENSED and
                    p['origin']==f'https://{u.hostname}' and
                    slots.stamp(p['valid_from'])<=now<slots.stamp(p['valid_until']))
            chosen[name]=p
        require(len({p['provider_identity'] for p in chosen.values()})==2 and
                len({p['origin'] for p in chosen.values()})==2)
        return chosen
    except Exception:
        raise PolicyHold('RETAINED_PRICE_SOURCE_NOT_ELIGIBLE') from None

def import_prices(profile,raw,*,expected,scheduled_close):
    """Independently resolve one canonical licensed record, never a URL/HTTP."""
    try:
        require(profile['role']=='admission_eligible' and profile['adapter']==LICENSED and
                type(raw) is bytes and 0<len(raw)<=24000)
        obs=slots.parse(raw)
        require(type(obs) is dict and set(obs)==set(expected)|{
            'schema_version','kind','provider_identity','scheduled_close_et','ohlc'})
        require(type(obs['schema_version']) is int and obs['schema_version']==1 and
                obs['kind']==LICENSED and obs['provider_identity']==profile['provider_identity'] and
                scheduled_close in ('13:00','16:00') and obs['scheduled_close_et']==scheduled_close and
                slots.key(expected)==expected and all(obs[k]==v for k,v in expected.items()))
        slots._stage_prices(obs['ohlc']);return obs['ohlc']
    except Exception:
        raise PolicyHold('RETAINED_PRICE_SOURCE_NOT_ELIGIBLE') from None
