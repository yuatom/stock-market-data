from datetime import datetime,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import base64,subprocess
from scripts import retained_price_only_source_trust as T
from scripts import retained_price_only_slots as S

D='2026-01-12'
EXPECTED={'open':'10.05','high':'12.18','low':'9.22','close':'11.11'}
SP='https://stockanalysis.com/etf/xqx/history/'
BC='https://raymondjames.websol.barchart.com/?symbol=XQX&region=US&module=etfDetail&1_selected=etfQuote'
def table(labels,row):
    return '<table><tr>'+''.join('<td>'+x+'</td>' for x in labels)+'</tr>'+row+'</table>'
def row(date='Jan 12, 2026'):
    return '<tr>'+''.join('<td>'+cell+'</td>' for cell in (date,'10.05','12.18','9.22','11.11','8.00','0','999'))+'</tr>'
def sp(rows):
    return f'<meta property="og:url" content="{SP}"><meta property="og:title" content="XQX Historical Stock Price Data"><title>XQX Historical Stock Price Data</title>'+table(T.SP_HEADERS,rows)
def bc(rows):
    return '<span class="exchange">(NASDAQ: XQX)</span><div id="detailedQuote"><div class="header">Detailed Quote - Past 5 Days</div>'+table(T.BC_HEADERS,rows)+'</div>'

class ExactRaw(TestCase):
    def test_source_specific_columns_and_identity(self):
        images=[(T.SP,SP,sp(row())),(T.BC,BC,bc(row('01/12/26')))]
        for source,url,html in images:
            self.assertEqual(T.extract_exact_prices(source,url,html.encode(),symbol='XQX',date=D),EXPECTED)
        for source,url,html in ((T.SP,SP,sp(row()+row())),(T.SP,SP,sp(row()).replace('XQX','AAA')),
                                (T.BC,BC,bc(row('01/12/26')).replace('Past 5 Days','Quote')),
                                (T.BC,BC,bc(row('01/13/26'))),
                                (T.BC,BC,bc(row('01/12/26')).replace('<td>Last</td>','<td>Close</td>'))):
            with self.subTest(source=source,html=html),self.assertRaises(T.SourceHold):
                T.extract_exact_prices(source,url,html.encode(),symbol='XQX',date=D)

class Approval(TestCase):
    def test_pinned_issuer_signature_expiry_and_scope(self):
        # Public RFC8032 test seed; never a provisioned production authority key.
        seed=bytes.fromhex('9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60')
        known_public=bytes.fromhex('302a300506032b6570032100d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a')
        scope=['a'*64]
        proof=S.encode(dict(schema_version=1,kind='qualified_vendor_observations',
            status='independently_approved_price_only',issuer='review_fixture',compute_sha='b'*40,approved_slots=scope,
            issued_at='2026-01-12T23:00:00Z',expires_at='2026-01-13T01:00:00Z',observations=[]))
        policy=S.encode(dict(schema_version=1,kind='retained_price_source_issuers',keys=[
            dict(issuer='review_fixture',public_key_der=base64.b64encode(known_public).decode(),
                 not_before='2026-01-01T00:00:00Z',not_after='2026-02-01T00:00:00Z')]))
        openssl='/opt/homebrew/opt/openssl@3/bin/openssl' if Path('/opt/homebrew/opt/openssl@3/bin/openssl').is_file() else '/usr/bin/openssl'
        with TemporaryDirectory() as tmp:
            tmp=Path(tmp);key=tmp/'key';payload=tmp/'payload';sig=tmp/'sig'
            key.write_bytes(bytes.fromhex('302e020100300506032b657004220420')+seed)
            payload.write_bytes(T.DOMAIN+proof)
            subprocess.run([openssl,'pkeyutl','-sign','-inkey',str(key),'-keyform','DER','-rawin',
                '-in',str(payload),'-out',str(sig)],check=True,capture_output=True)
            signature=sig.read_bytes()
        now=datetime(2026,1,13,0,0,tzinfo=timezone.utc)
        with patch.object(T,'OPENSSL',openssl):
            self.assertEqual(T.verify_approval(policy,proof,signature,compute='b'*40,scope=scope,now=now)['issuer'],'review_fixture')
            for pol,raw,sign,expected,clock in [
                (T.EMPTY_POLICY,proof,signature,scope,now),
                (policy,proof,signature,['c'*64],now),
                (policy,proof.replace(b'review_fixture',b'other_issuer'),signature,scope,now),
                (policy,proof,bytes(64),scope,now),
                (policy,proof,signature,scope,now.replace(hour=2))]:
                with self.subTest(pol=pol,expected=expected,clock=clock),self.assertRaises(T.SourceHold):
                    T.verify_approval(pol,raw,sign,compute='b'*40,scope=expected,now=clock)
