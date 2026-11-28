"""Verify independently approved source evidence; no network or key provisioning."""
import base64
from datetime import date as civil_date, timedelta
from html.parser import HTMLParser
from pathlib import Path
import re, subprocess
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit
from scripts import retained_price_only_slots as slots

SP='stockanalysis_spglobal_historical_html'
BC='barchart_raymond_james_historical_html'
SP_HEADERS=('Date','Open','High','Low','Close','Adj. Close','Change','Volume')
BC_HEADERS=('Date','Open','High','Low','Last','Change','% Change','Volume')
DOMAIN=b'RETAINED-PRICE-ONLY-SOURCE-ADMISSION-V1\0'
TRUST_PATH='config/retained-price-only-source-trust.json'
EMPTY_POLICY=slots.encode({'schema_version':1,'kind':'retained_price_source_issuers','keys':[]})
OPENSSL='/usr/bin/openssl'
SPKI=bytes.fromhex('302a300506032b6570032100')
MONTHS=('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec')

class SourceHold(ValueError):pass

def need(value):
    if not value:raise SourceHold('RETAINED_SOURCE_TRUST_HOLD')

def _strict_keys(obj,names):
    need(type(obj) is dict and set(obj)==set(names))

def verify_approval(trust_raw,raw,signature,*,compute,scope,now):
    try:
        need(len(trust_raw)<=16000 and 0<len(raw)<=128000 and type(signature) is bytes and len(signature)==64)
        trust=slots.parse(trust_raw)
        _strict_keys(trust,('schema_version','kind','keys'))
        need(type(trust['schema_version']) is int and trust['schema_version']==1 and
             trust['kind']=='retained_price_source_issuers' and type(trust['keys']) is list and 0<len(trust['keys'])<=8)
        proof=slots.parse(raw)
        _strict_keys(proof,('schema_version','kind','status','issuer','compute_sha','approved_slots','issued_at','expires_at','observations'))
        need(type(proof['schema_version']) is int and proof['schema_version']==1 and slots.oid(compute) and
             proof['kind']=='qualified_vendor_observations' and proof['status']=='independently_approved_price_only' and
             proof['compute_sha']==compute and type(scope) is list and 0<len(scope)<=8 and scope==sorted(set(scope)) and
             all(re.fullmatch('[0-9a-f]{64}',x) for x in scope) and proof['approved_slots']==scope)
        issued=slots.stamp(proof['issued_at']);expires=slots.stamp(proof['expires_at'])
        need(issued<=now<expires<=issued+timedelta(days=7))
        by_id={}
        for key in trust['keys']:
            _strict_keys(key,('issuer','public_key_der','not_before','not_after'))
            name=key['issuer']
            need(type(name) is str and re.fullmatch(r'[a-z][a-z0-9_]{0,63}',name) and name not in by_id)
            raw_key=base64.b64decode(key['public_key_der'],validate=True)
            need(len(raw_key)==44 and raw_key.startswith(SPKI) and
                 base64.b64encode(raw_key).decode()==key['public_key_der'])
            by_id[name]=raw_key,slots.stamp(key['not_before']),slots.stamp(key['not_after'])
        key,start,end=by_id[proof['issuer']]
        need(start<=issued<expires<=end)
        with TemporaryDirectory(prefix='retained-source-signature-') as work:
            temp=Path(work)
            public,temp_data,temp_sig=(temp/x for x in ('public.der','scope','signature'))
            public.write_bytes(key);temp_data.write_bytes(DOMAIN+raw);temp_sig.write_bytes(signature)
            result=subprocess.run([OPENSSL,'pkeyutl','-verify','-pubin','-inkey',str(public),'-keyform','DER',
                                   '-rawin','-in',str(temp_data),'-sigfile',str(temp_sig)],
                                  capture_output=True,env={'PATH':'/usr/bin:/bin'},timeout=10)
            need(result.returncode==0)
        return proof
    except Exception:
        raise SourceHold('RETAINED_SOURCE_TRUST_HOLD') from None

def _line(parts):return ' '.join(''.join(parts).split())

class _Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.table_stack=[];self.tables=[];self.divs=[];self.header=None;self.headers=[]
        self.title=None;self.titles=[];self.exchange=None;self.exchanges=[];self.meta={}
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs);classes=set((attrs.get('class') or '').split())
        if tag=='div':
            self.divs.append(attrs.get('id'))
            if 'detailedQuote' in self.divs and 'header' in classes:
                self.header=(len(self.divs),[])
        elif tag=='span' and 'exchange' in classes:
            self.exchange=[]  # known simple span, captured in full before close
        elif tag=='title':
            self.title=[]
        elif tag=='meta' and attrs.get('property') in ('og:title','og:url'):
            self.meta.setdefault(attrs['property'],[]).append(attrs.get('content'))
        elif tag=='table':
            self.table_stack.append(dict(detail='detailedQuote' in self.divs,label=self.headers[-1] if self.headers else None,rows=[],row=None,cell=None))
        elif self.table_stack:
            t=self.table_stack[-1]
            if tag=='tr':t['row']=[]
            elif tag in ('td','th') and t['row'] is not None:t['cell']=[]
    def handle_data(self,data):
        for capture in (self.title,self.exchange,self.header[1] if self.header else None):
            if capture is not None:capture.append(data)
        if self.table_stack and self.table_stack[-1]['cell'] is not None:self.table_stack[-1]['cell'].append(data)
    def handle_endtag(self,tag):
        if tag=='div' and self.divs:
            if self.header is not None and self.header[0]==len(self.divs):
                self.headers.append(_line(self.header[1]));self.header=None
            self.divs.pop()
        elif tag=='title' and self.title is not None:
            self.titles.append(_line(self.title));self.title=None
        elif tag=='span' and self.exchange is not None:
            self.exchanges.append(_line(self.exchange));self.exchange=None
        elif tag=='table' and self.table_stack:
            self.tables.append(self.table_stack.pop())
        elif self.table_stack:
            t=self.table_stack[-1]
            if tag in ('td','th') and t['cell'] is not None and t['row'] is not None:
                t['row'].append(_line(t['cell']));t['cell']=None
            elif tag=='tr' and t['row'] is not None:
                t['rows'].append(tuple(t['row']));t['row']=None;t['cell']=None

def extract_exact_prices(source,url,raw,*,symbol,date):
    """Select exactly one unambiguous regular-day row in the adopted page shape."""
    try:
        need(type(raw) is bytes and 0<len(raw)<=512000 and re.fullmatch(r'[A-Z][A-Z0-9.-]{0,14}',symbol))
        d=civil_date.fromisoformat(date)
        need(d.isoformat()==date and 2000<=d.year<=2099)
        u=urlsplit(url)
        need(u.scheme=='https' and u.username is None and u.password is None and u.port is None and not u.fragment)
        parsed=_Tables();parsed.feed(raw.decode('utf-8',errors='strict'));parsed.close()
        if source==SP:
            need(u.hostname=='stockanalysis.com' and u.path==f'/etf/{symbol.lower()}/history/' and not u.query)
            headline=f'{symbol} Historical Stock Price Data'
            need(parsed.meta.get('og:url')==[url] and parsed.meta.get('og:title')==[headline] and parsed.titles==[headline])
            hdr=SP_HEADERS; render=f'{MONTHS[d.month-1]} {d.day}, {d.year}'
            tables=[t for t in parsed.tables if t['rows'] and t['rows'][0]==hdr]
        elif source==BC:
            need(u.hostname=='raymondjames.websol.barchart.com' and u.path=='/')
            query=parse_qs(u.query,strict_parsing=True,keep_blank_values=True)
            need(all(query.get(k)==[v] for k,v in {'symbol':symbol,'region':'US','module':'etfDetail','1_selected':'etfQuote'}.items()))
            need(parsed.exchanges==[f'(NASDAQ: {symbol})'] and parsed.headers.count('Detailed Quote - Past 5 Days')==1)
            hdr=BC_HEADERS; render=f'{d.month:02d}/{d.day:02d}/{d.year%100:02d}'
            tables=[t for t in parsed.tables if t['detail'] and t['label']=='Detailed Quote - Past 5 Days' and t['rows'] and t['rows'][0]==hdr]
        else:raise SourceHold('RETAINED_SOURCE_TRUST_HOLD')
        need(len(tables)==1)
        matches=[r for r in tables[0]['rows'][1:] if r and r[0]==render]
        need(len(matches)==1 and len(matches[0])==len(hdr))
        cells=matches[0][1:5]
        need(all(re.fullmatch(r'\$?(?:0|[1-9]\d{0,7})\.\d{2}',c) for c in cells))
        prices=dict(zip(slots.FIELDS,(c.removeprefix('$') for c in cells)))
        slots._stage_prices(prices)
        return prices
    except Exception:
        raise SourceHold('RETAINED_SOURCE_TRUST_HOLD') from None
