from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from scripts.retained_price_only_slots import slot_key,plan,commit_atomically,Hold,evidence_path,record_path
import subprocess, json

IDENT=dict(mic='XNMS',symbol='XQX',asset_class='etf',currency='USD',session='regular',basis='split_adjusted_price_only')
CLASS=dict(schema_version=1,kind='admitted_regular_daily_price_backup',purpose='prior_session_close_only',native_provider_receipt=False,admitted_at='2026-11-04T00:00:00Z')
P=dict(open='10.05',high='12.01',low='9.01',close='11.02')
D1='2026-11-02';D2='2026-11-03'
def git(path,*args,check=True):
    return subprocess.run(['git','-C',str(path),'-c','user.name=Test','-c','user.email=t@example.invalid',*args],
                          check=check,capture_output=True,text=True)
def encode(o):return (json.dumps(o,sort_keys=True,separators=(',',':'))+'\n').encode()
def package(repo,date,prices=P):
    obs={}
    evidence=[]
    row=CLASS|IDENT|dict(trade_date=date,scheduled_close_et='16:00',ohlc=prices)
    for source in ('testa','testb'):
        path=evidence_path(row,source)
        payload=encode(IDENT|dict(schema_version=1,kind='public_regular_daily_price_observation',trade_date=date,
            scheduled_close_et='16:00',source_id=source,source_basis='split_adjusted_explicit',observed_at='2026-11-03T22:00:00Z',url=f'https://{source}.example.invalid/price',ohlc=prices))
        import hashlib
        oid=hashlib.sha1(f'blob {len(payload)}\0'.encode()+payload).hexdigest()
        evidence.append(dict(source_id=source,path=path,blob_sha=oid,sha256=hashlib.sha256(payload).hexdigest()))
        obs[path]=payload
    return row|{'evidence':evidence},obs

class Claims(TestCase):
    def setUp(self):
        self.d=TemporaryDirectory();self.addCleanup(self.d.cleanup)
        self.root=Path(self.d.name)
        self.store=self.root/'store';self.repo=self.root/'repo'
        self.store.mkdir();self.repo.mkdir()
        git(self.store,'init','-q','--bare','-b','main')
        git(self.repo,'init','-qb','main')
        (self.repo/'sentinel').write_text('immutable baseline\n')
        git(self.repo,'add','.');git(self.repo,'commit','-qm','baseline')
        git(self.repo,'remote','add','origin',str(self.store))
        git(self.repo,'push','-q','origin','main')
        self.base=git(self.repo,'rev-parse','HEAD').stdout.strip()
        git(self.repo,'push','-q','origin',f'{self.base}:refs/tags/market-data-read/{self.base}')
        self.observations={'testa':'https://testa.example.invalid/price','testb':'https://testb.example.invalid/price'}
    def proposal(self,base,*dates):
        items=tuple(package(self.repo,d) for d in dates)
        return plan(self.repo,base,items,source_urls=self.observations,session_close=dict.fromkeys(dates,'16:00'),legacy_occupied=set(),request_id='101-1')
    def publish(self,p,check=lambda:True):
        return commit_atomically(self.repo,p,before_push=check)
    def test_two_dates_reuse_same_lane_and_overlap_noop_or_hold(self):
        a=self.proposal(self.base,D1);first=self.publish(a)
        self.assertTrue(git(self.store,'show',first+':data/market-data/'+record_path(package(self.repo,D1)[0])).stdout)
        self.assertIsNone(self.publish(self.proposal(first,D1)))
        with self.assertRaises(Hold):self.proposal(first,D1,D2)
        second=self.publish(self.proposal(first,D2))
        self.assertEqual(git(self.store,'rev-parse','main').stdout.strip(),second)
        for date in (D1,D2):
            row,_=package(self.repo,date)
            self.assertEqual(git(self.store,'show-ref','--verify',slot_key(row)).returncode,0)
    def test_stale_base_or_authority_reject_without_partial_refs(self):
        a=self.proposal(self.base,D1);b=self.proposal(self.base,D2)
        with self.assertRaises(Hold):self.publish(a,lambda:False)
        self.assertEqual(git(self.store,'rev-parse','main').stdout.strip(),self.base)
        self.publish(a)
        with self.assertRaises(Hold):self.publish(b)
        self.assertNotEqual(git(self.store,'show-ref','--verify',slot_key(package(self.repo,D2)[0]),check=False).returncode,0)
    def test_wrong_source_date_volume_and_legacy_reject(self):
        row,evidence=package(self.repo,D1)
        for bad in (row|{'volume':0},row|{'trade_date':D2},
                    row|{'ohlc':dict(P,close='10.00')}):
            with self.assertRaises(Hold):
                plan(self.repo,self.base,((bad,evidence),),source_urls=self.observations,session_close={D1:'16:00',D2:'16:00'},legacy_occupied=set(),request_id='101-1')
        with self.assertRaises(Hold):
            plan(self.repo,self.base,((row,evidence),),source_urls=self.observations,session_close={D1:'16:00'},legacy_occupied={slot_key(row)},request_id='101-1')

    def test_concurrent_claim_fails_atomic_all_slots(self):
        p=self.proposal(self.base,D1,D2)
        row1,_=package(self.repo,D1);row2,_=package(self.repo,D2)
        def race():
            # Simulates a foreign create-only reservation after our absence precheck.
            git(self.store,'update-ref',slot_key(row1),self.base)
            return True
        with self.assertRaises(Hold):self.publish(p,race)
        self.assertEqual(git(self.store,'rev-parse','main').stdout.strip(),self.base)
        self.assertNotEqual(git(self.store,'show-ref','--verify',slot_key(row2),check=False).returncode,0)
        self.assertEqual(len(git(self.store,'tag','-l','market-data-read/*').stdout.splitlines()),1)

    def test_lost_ack_not_retried_and_existing_verified(self):
        from unittest.mock import patch
        from scripts import retained_price_only_slots as R
        p=self.proposal(self.base,D1)
        original=R.git
        writes=[]
        def lost_ack(root,*args,**kw):
            res=original(root,*args,**kw)
            if args[:1]==('push',):
                writes.append(True)
                raise TimeoutError('synthetic ack lost')
            return res
        with patch.object(R,'git',side_effect=lost_ack),self.assertRaises(Hold):
            self.publish(p)
        self.assertEqual(writes,[True])
        current=git(self.store,'rev-parse','main').stdout.strip()
        self.assertNotEqual(current,self.base)
        self.assertIsNone(self.publish(self.proposal(current,D1)))

    def test_unstated_source_basis_needs_exact_independent_normalization(self):
        import hashlib
        row,payload=package(self.repo,D1)
        updated=[]
        for i,ref in enumerate(row['evidence']):
            if i==1:
                source=json.loads(payload[ref['path']])
                source['source_basis']='regular_prices_post_split_normalized'
                raw=encode(source)
                payload[ref['path']]=raw
                ref=ref|dict(blob_sha=hashlib.sha1(f'blob {len(raw)}\0'.encode()+raw).hexdigest(),
                             sha256=hashlib.sha256(raw).hexdigest())
                normalization={(D1,ref['source_id']):ref['sha256']}
            updated.append(ref)
        row['evidence']=updated
        def prepare(evidence):
            return plan(self.repo,self.base,((row,payload),),source_urls=self.observations,
                session_close={D1:'16:00'},legacy_occupied=set(),request_id='101-1',normalized_sources=evidence)
        with self.assertRaises(Hold):prepare({})
        self.assertFalse(prepare(normalization).noop)

    def test_same_prices_different_evidence_holds_not_price_conflict(self):
        from scripts import retained_price_only_slots as R
        original=self.publish(self.proposal(self.base,D1))
        row,payload=package(self.repo,D1)
        one=row['evidence'][0]
        parsed=json.loads(payload[one['path']]);parsed['observed_at']='2026-11-03T22:00:01Z'
        raw=encode(parsed);payload[one['path']]=raw;one.update(blob_sha=R.blob(raw),sha256=R.sha(raw))
        before=git(self.store,'show-ref').stdout
        with self.assertRaisesRegex(Hold,'RETAINED_PRICE_ONLY_SAME_PRICES_AUTHORITY_CHANGED'):
            plan(self.repo,original,((row,payload),),source_urls=self.observations,
                 session_close={D1:'16:00'},legacy_occupied=set(),request_id='103-1')
        self.assertEqual(git(self.store,'show-ref').stdout,before)
        self.assertIsNone(self.publish(self.proposal(original,D1)))
