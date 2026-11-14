import copy
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch
import pytest
import maintain_fixed_symbols as maintenance
import market_data_collectors as collectors
import backfill_market_data_tail as tail

# Exact production ledger immediately before run 43527726597's first debit.
MINUTES = dict(zip(['15:36','15:37','15:38','19:23','19:24','19:25','19:26','20:58','21:06','21:27','22:08','22:09','22:10','22:11','22:12','22:13'], [8,8,6,8,8,8,8,4,3,2,8,8,8,8,8,3]))
BEFORE = {'schema_version':1,'trade_date':'2026-11-13','credits':158,'minutes':{'2026-11-13T'+k+'Z':v for k,v in MINUTES.items()}}
NEW = '2026-11-13T23:32Z'

def emitted_debit(before, stamp='2026-11-13T23:32:01+00:00'):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value=datetime.fromisoformat(stamp)
            return value if tz is None else value.astimezone(tz)
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp); path=collectors._budget_path(root,'2026-11-13');path.parent.mkdir(parents=True);path.write_text(json.dumps(before))
        with patch.object(collectors,'datetime',Clock):
            collectors.consume_twelve_credit(root,'2026-11-13',amount=1,per_day=200,per_minute=8)
        return json.loads(path.read_text())

def test_real_writer_rolloff_is_exactly_one_debit():
    after=emitted_debit(BEFORE)
    assert len(after['minutes'])==16 and after['credits']==159
    assert '2026-11-13T15:36Z' not in after['minutes']
    maintenance._one_debit(copy.deepcopy(BEFORE),after)

@pytest.mark.parametrize('case',['retained_count','wrong_drop','extra_drop','extra_new','credit_two','minute_two','missing_new','bool_count'])
def test_rolloff_does_not_accept_tampering(case):
    after=emitted_debit(BEFORE)
    if case=='retained_count':after['minutes']['2026-11-13T15:37Z']=7
    elif case=='wrong_drop':after['minutes']['2026-11-13T15:36Z']=8;del after['minutes']['2026-11-13T15:37Z']
    elif case=='extra_drop':del after['minutes']['2026-11-13T15:37Z']
    elif case=='extra_new':after['minutes']['2026-11-13T23:33Z']=1
    elif case=='credit_two':after['credits']=160
    elif case=='minute_two':after['minutes'][NEW]=2
    elif case=='missing_new':del after['minutes'][NEW]
    elif case=='bool_count':after['minutes'][NEW]=True
    with pytest.raises(maintenance.r.RecoveryError,match='budget_state_invalid'):maintenance._one_debit(BEFORE,after)

def test_existing_minute_and_short_ledger_still_work():
    before=copy.deepcopy(BEFORE); before['minutes']={'2026-11-13T23:32Z':2}
    maintenance._one_debit(before,emitted_debit(before))

@pytest.mark.parametrize('status,expected',[('PASS',0),('NOOP',0),('PARTIAL',2),('BLOCKED',2)])
def test_cli_returns_failure_for_incomplete_recovery(status,expected):
    sha='a'*40
    env={'GITHUB_ACTIONS':'true','GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_REPOSITORY':'yuatom/stock-market-data','GITHUB_REF':'refs/heads/main','GITHUB_SHA':sha,'GITHUB_RUN_ATTEMPT':'1','GITHUB_ACTOR':'yuatom','GITHUB_TRIGGERING_ACTOR':'yuatom'}
    from types import SimpleNamespace
    h=SimpleNamespace(collectors=SimpleNamespace(load_yaml=lambda path:{'collector':{'twelve_data_daily':{'bounded_overlap_revision_policy':'price_only_volume_blocked_v1'}}}),collection=SimpleNamespace(load_collection_universe=lambda path:{}))
    args=['--execution-date','2026-11-13','--historical-cutoff','2026-11-11','--compute-sha',sha,'--store-sha',sha,'--store-repo','unused','--request-id','43527726597-1','--fixed-selection','{}']
    with patch.dict(os.environ,env),patch.object(maintenance.a,'load_helpers',return_value=h),patch.object(tail,'parse_fixed_selection',return_value={}),patch.object(maintenance,'maintain_append',return_value={'status':status}):
        assert tail.main(args)==expected
