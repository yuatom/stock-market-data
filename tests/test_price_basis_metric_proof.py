"""Synthetic independent Compute proof producer contracts."""
import copy
import pytest
from test_provider_basis_snapshot import fixture_repo
from scripts.build_price_basis_metric_proof import build_price_basis_proof
from scripts.provider_basis_snapshot import BasisError


def build(repo,sel,auth,**kwargs):
    return build_price_basis_proof(repo,sel,auth,required_sessions=kwargs.pop('required_sessions',['2026-01-01','2026-01-02','2026-01-05','2026-01-06']),knowledge_cutoff=kwargs.pop('knowledge_cutoff','2026-01-07T14:00:00Z'),compute_sha='c'*40,scope=kwargs.pop('scope','prior_session_core_context'),**kwargs)


def test_proof_binds_all_basis_objects_preserves_existing_formula(tmp_path):
    repo,sel,auth,_,_=fixture_repo(tmp_path)
    p=build(repo,sel,auth)
    assert p['selector']=={k:v for k,v in sel.items() if k!='read_sha'}
    assert p['compute_sha']=='c'*40
    assert p['result']['returns_pct']=={'1session':8.333333,'3session':30.0,'5session':None}
    assert p['result']['volume']=={'status':'blocked','reason':'BLOCKED_UNRESOLVED'}
    assert p['formula_contract']=='stock_dairy_calculation_policy_v2'
    assert p['result']['prior_record_count']==3
    assert p['result']['target_close']==13


@pytest.mark.parametrize('args',[{'scope':'volume'},{'required_sessions':['2026-01-07']},{'knowledge_cutoff':'2026-01-06T20:00:00Z'}])
def test_proof_rejects_missing_session_later_vintage_or_unsupported_scope(tmp_path,args):
    repo,sel,auth,_,_=fixture_repo(tmp_path)
    with pytest.raises((BasisError,ValueError)):build(repo,sel,auth,**args)
