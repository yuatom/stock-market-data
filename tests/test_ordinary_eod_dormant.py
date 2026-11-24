"""Offline real Git entry, no provider calls or authorization claims."""
from contextlib import redirect_stdout
import datetime as dt
import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts import market_data_collectors as collector
from scripts import market_data_store
from scripts.daily_basis_store import LocalGitStore
from scripts.private_authority_input import STORE_REPOSITORY
from scripts.ordinary_eod_local_state import OrdinaryEodLocalState, PREFIX

ROOT = Path(__file__).resolve().parents[1]
DAY, SESSION = "2041-06-04", "2041-06-03"
SYMBOL, FINITE = "TEST1", "TEST2"
ET = ZoneInfo("America/New_York")
START = dt.datetime(2041,6,4,7,5,tzinfo=ET)
STOP = START.replace(hour=8,minute=30)


def config(*, claimed=True):
    value = collector.load_yaml(ROOT/"config/market-data-store.yaml")
    if claimed:
        value["collector"]["ordinary_eod_admission"] = "claimed_v1"
    return value


def watch(*symbols):
    return dict(core_watchlist=list(symbols),instruments={s:{"asset_class":"stocks"} for s in symbols})


def collect(root, *, state=None, claimed=True, finite=False):
    return collector.collect_previous_session_eod(trade_date=DAY,store_root=root,
        watchlist=watch(SYMBOL,FINITE) if finite else watch(SYMBOL),completeness={},
        config=config(claimed=claimed),access=collector.load_yaml(ROOT/"config/market-data-collector-access.yaml"),
        ordinary_eod_state=state)


def rows():
    prev=dt.date.fromisoformat(SESSION)
    out=[dict(trade_date=(prev-dt.timedelta(days=i)).isoformat(),open=20,high=24,low=19,close=23,volume=10)
         for i in range(40,-1,-1) if (prev-dt.timedelta(days=i)).weekday()<5]
    return [*out,dict(trade_date=DAY,open=20,high=24,low=19,close=21,volume=10)]


def initialize(repo):
    repo.mkdir()
    def git(*args, data=None):
        env=dict(os.environ,GIT_CONFIG_NOSYSTEM="1",GIT_CONFIG_GLOBAL="/dev/null",GIT_ALLOW_PROTOCOL="")
        return subprocess.check_output(("git","-C",str(repo),*args),input=data,text=True,env=env).strip()
    git("init","-q","-b","main","--template=")
    git("config","remote.origin.url",f"https://github.com/{STORE_REPOSITORY}")
    marker=git("hash-object","-w","--stdin",data="synthetic nonempty baseline\n")
    raw=git("mktree",data=f"100644 blob {marker}\tfixture-marker\n")
    start=git("-c","user.name=Fixture","-c","user.email=fixture@example.invalid","commit-tree",raw,data="fixture\n")
    git("update-ref","refs/heads/main",start)
    out=repo/PREFIX
    out.mkdir(parents=True)
    return out,start


def adapter(repo, fetch, clock=lambda:START, symbol=SYMBOL):
    return OrdinaryEodLocalState(repo=repo,previous_session=SESSION,cutoff=STOP,
        scope=((symbol,"stocks"),),clock=clock,offline_fetch=fetch)


class OrdinaryEodDormantTest(unittest.TestCase):
    def test_real_cli_reserved_policy_is_unregistered_nonzero_and_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp)
            (d/"config.json").write_text(json.dumps(config()))
            (d/"watch.json").write_text(json.dumps(watch(SYMBOL)))
            (d/"empty.yaml").write_text("{}")
            out=d/"output"
            with redirect_stdout(io.StringIO()) as log, patch.object(
                    collector,"fetch_twelve_daily",side_effect=AssertionError("provider boundary crossed")) as provider:
                rc=collector.main(["--mode","previous_session_eod","--trade-date",DAY,"--store-root",str(out),
                    "--config",str(d/"config.json"),"--watchlist",str(d/"watch.json"),
                    "--access-config",str(d/"empty.yaml"),"--data-completeness",str(d/"empty.yaml")])
            self.assertEqual(rc,2)
            self.assertEqual(json.loads(log.getvalue())["status"],"held_ordinary_eod_unregistered")
            self.assertFalse(out.exists())
            provider.assert_not_called()

    def test_real_collector_commits_once_before_effect_and_keeps_finite_delegation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)/"repo"
            out,origin=initialize(repo)
            received=[]
            def fetch(symbol, **kw):
                s=LocalGitStore(repo)
                tip=s.head()
                self.assertNotEqual(tip,origin)
                self.assertEqual(json.loads(s.read(tip, state.claim))["state"],"CLAIMED_NO_REPLAY")
                self.assertEqual(json.loads(s.read(tip,BUDGET))["credits"],1)
                received.append((symbol,tip))
                return rows()
            state=adapter(repo,fetch)
            with patch.object(collector,"_load_daily_basis_owner",return_value={FINITE:"OWNED_IMMUTABLE_DAILY"}), \
                    patch.object(collector,"consume_twelve_credit",side_effect=AssertionError("legacy debit")) as legacy, \
                    patch.object(collector,"fetch_twelve_daily",side_effect=AssertionError("live provider")) as external:
                item=collect(out,state=state,finite=True)
                second=collect(out,state=adapter(repo,fetch),finite=True)
                legacy.assert_not_called()
                external.assert_not_called()
            self.assertEqual(item["status"],"ok")
            self.assertEqual(item["delegated_daily_symbols"],[{"symbol":FINITE,"status":"OWNED_IMMUTABLE_DAILY"}])
            self.assertEqual(received,[received[0]])
            self.assertEqual(received[0][0],SYMBOL)
            s=LocalGitStore(repo)
            tip=s.head()
            self.assertEqual(s.g.commit(tip)[1],(received[0][1],))
            self.assertEqual(s.ref("refs/tags/market-data-read/"+tip),tip)
            self.assertEqual(tip,item["ordinary_eod_local_store_commit"])
            self.assertEqual(json.loads(s.read(tip,BUDGET))["credits"],1)
            self.assertFalse((repo/BUDGET).exists())
            self.assertEqual(market_data_store.read_daily_series(out,provider="twelve_data_basic",symbol=SYMBOL)[-1]["trade_date"],SESSION)
            self.assertEqual(second["status"],"held_ordinary_eod_consumed_no_replay")

    def test_old_default_uses_existing_path_and_caps(self):
        with tempfile.TemporaryDirectory() as t, patch.dict(os.environ,{"TWELVE_DATA_API_KEY":"fixture-only"}), \
                patch.object(collector,"fetch_twelve_daily",return_value=rows()) as offline:
            root=Path(t)
            result=collect(root,claimed=False)
            self.assertEqual(result["status"],"ok")
            self.assertEqual(json.loads(collector._budget_path(root,DAY).read_bytes())["credits"],1)
            self.assertNotIn("ordinary_eod_local_store_commit",result)
            self.assertEqual(offline.call_count,1)

    def test_unknown_effect_remains_consumed_without_retry_or_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)/"repo"
            out,_=initialize(repo)
            attempts=[]
            def unknown(symbol, **kw):
                attempts.append(symbol)
                raise RuntimeError("redacted synthetic uncertainty")
            state=adapter(repo, unknown)
            first=collect(out,state=state)
            tip=LocalGitStore(repo).head()
            second=collect(out,state=adapter(repo,unknown))
            s=LocalGitStore(repo)
            self.assertEqual((first["status"],second["status"]),
                ("held_ordinary_eod_state_unknown","held_ordinary_eod_consumed_no_replay"))
            self.assertEqual(attempts,[SYMBOL])
            self.assertEqual(s.head(),tip)
            self.assertFalse(s.exists(tip,state.terminal))
            self.assertEqual(json.loads(s.read(tip,BUDGET))["credits"],1)

    def test_terminal_does_not_rebase_across_an_independent_main_advance(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)/"repo"
            out,_=initialize(repo)
            observed=[]
            def advances(symbol, **kw):
                external=LocalGitStore(repo)
                with external.lock():
                    old=external.head()
                    observed.append(external.publish(old,{"synthetic-other.json":b"{}\n"},replace=set()))
                return rows()
            state=adapter(repo,advances)
            result=collect(out,state=state)
            self.assertEqual(result["status"],"held_ordinary_eod_state_unknown")
            s=LocalGitStore(repo)
            self.assertEqual(s.head(),observed[0])
            self.assertFalse(s.exists(s.head(),state.terminal))
            self.assertEqual(json.loads(s.read(s.head(),BUDGET))["credits"],1)
            self.assertEqual(collect(out,state=adapter(repo,advances))["status"],
                             "held_ordinary_eod_consumed_no_replay")
            self.assertEqual(len(observed),1)

    def test_existing_daily_and_minute_caps_prevent_effect(self):
        minute=START.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        for credits,minutes in ((200,{}),(8,{minute:8})):
            with self.subTest(credits=credits), tempfile.TemporaryDirectory() as tmp:
                repo=Path(tmp)/"repo"
                out,_=initialize(repo)
                s=LocalGitStore(repo)
                budget=json.dumps(dict(schema_version=1,trade_date=DAY,credits=credits,minutes=minutes)).encode()
                target=repo/BUDGET
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(budget)
                with s.lock():
                    s.publish(s.head(),{BUDGET:budget},replace=set())
                crossed=[]
                def no(*a,**kw):
                    crossed.append(True)
                    raise AssertionError("cap reached provider boundary")
                state=adapter(repo,no)
                result=collect(out,state=state)
                self.assertEqual(result["status"],"held_ordinary_eod_state_unknown")
                self.assertEqual(crossed,[])
                self.assertEqual(s.read(s.head(),BUDGET),budget)
                self.assertFalse(s.exists(s.head(),state.terminal))

    def test_mixed_scope_missing_exact_target_is_known_incomplete_no_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)/"repo"
            out,_=initialize(repo)
            seen=[]
            def fetch(symbol, **kw):
                seen.append(symbol)
                return rows() if symbol == SYMBOL else [bar for bar in rows() if bar["trade_date"] != SESSION]
            def state():
                return OrdinaryEodLocalState(repo=repo,previous_session=SESSION,cutoff=STOP,
                     scope=((SYMBOL,"stocks"),(FINITE,"stocks")),clock=lambda:START,offline_fetch=fetch)
            a=state()
            first=collect(out,state=a,finite=True)
            self.assertEqual(first["status"],"held_ordinary_eod_incomplete_no_replay",first["status"])
            s=LocalGitStore(repo)
            tip=s.head()
            terminal=json.loads(s.read(tip,a.terminal))
            self.assertEqual(terminal["state"],"INCOMPLETE_NO_REPLAY")
            self.assertEqual(terminal["missing"],[FINITE])
            self.assertEqual(terminal["outputs"],{})
            for symbol in (SYMBOL,FINITE):
                self.assertFalse(s.exists(tip,PREFIX+f"series/daily/twelve_data_basic/{symbol}/_index.json"))
            self.assertEqual(json.loads(s.read(tip,BUDGET))["credits"],2)
            self.assertEqual(collect(out,state=state(),finite=True)["status"],
                "held_ordinary_eod_consumed_no_replay")
            self.assertEqual(s.head(),tip)
            self.assertEqual(seen,[SYMBOL,FINITE])

    def test_preterminal_rewrite_with_valid_target_cannot_change_incumbents(self):
        for mutation in ("close","volume","lineage","other_scope","missing_row"):
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as tmp:
                    repo=Path(tmp)/"repo"
                    out,_=initialize(repo)
                    sample=[bar for bar in rows() if bar["trade_date"] < SESSION]
                    market_data_store.append_daily_bars(out,provider="twelve_data_basic",symbol=SYMBOL,
                        asset_class="stocks",series_semantics="daily_regular_ohlcv", adjustment_semantics="provider_reported",
                        lineage={"ingest_kind":"incumbent-evidence","volume_policy":"BLOCKED_UNRESOLVED"},bars=sample)
                    s=LocalGitStore(repo)
                    seed={p.relative_to(repo).as_posix():p.read_bytes() for p in out.rglob("*") if p.is_file()}
                    with s.lock():
                        s.publish(s.head(),seed,replace=set())
                    index = out/f"series/daily/twelve_data_basic/{SYMBOL}/_index.json"
                    original_diag = collector._write_private_eod_diagnostic
                    observed=[]
                    def rewrite(**kwargs):
                        original_diag(**kwargs)
                        doc=json.loads(index.read_bytes())
                        first=doc["shards"][0]
                        file=index.parent/first["path"]
                        data=json.loads(file.read_bytes())
                        if mutation in {"close","volume","missing_row"}:
                            if mutation == "close":
                                data["records"][0]["close"] = 22
                            elif mutation == "volume":
                                data["records"][0]["volume"] = 11
                            else:
                                del data["records"][0]
                                doc["record_count"] -= 1
                                doc["first_trade_date"] = data["records"][0]["trade_date"]
                            market_data_store._write_json(file,data)
                            doc["shards"][0]=market_data_store._shard_meta(first["path"],first["start"],data,first["sealed"])
                        elif mutation == "lineage":
                            doc["lineage_segments"][0].pop("volume_policy")
                        else:
                            unrelated=out/"collector-state/unrelated-synthetic.json"
                            unrelated.write_text("{}")
                        market_data_store._write_json(index,doc)
                        observed.append(market_data_store.read_daily_exact(out,provider="twelve_data_basic",
                            symbol=SYMBOL,trade_date=SESSION)["close"])
                    state=adapter(repo,lambda *a,**k: rows())
                    with patch.object(collector,"_write_private_eod_diagnostic",side_effect=rewrite):
                        first=collect(out,state=state)
                    self.assertEqual(observed,[23])
                    self.assertEqual(first["status"],"held_ordinary_eod_prepared_drift_no_replay")
                    tip=s.head()
                    receipt=json.loads(s.read(tip,state.terminal))
                    self.assertEqual(receipt["state"],"FAILED_PREPARED_DRIFT_NO_REPLAY")
                    self.assertEqual(receipt["outputs"],{})
                    for path, data in seed.items():
                        self.assertEqual(s.read(tip,path),data)
                    self.assertEqual(json.loads(s.read(tip,BUDGET))["credits"],1)
                    self.assertEqual(collect(out,state=adapter(repo,lambda *a,**k: rows()))["status"],
                        "held_ordinary_eod_consumed_no_replay")
                    self.assertEqual(s.head(),tip)

    def test_owned_common_quarantine_keeps_incumbent_volume_and_lineage(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)/"repo"
            out,_=initialize(repo)
            sample=[bar for bar in rows() if bar["trade_date"] < SESSION]
            market_data_store.append_daily_bars(out,provider="twelve_data_basic",symbol=SYMBOL,
                asset_class="stocks",series_semantics="daily_regular_ohlcv", adjustment_semantics="provider_reported",
                lineage={"ingest_kind":"incumbent-evidence"},bars=sample)
            s=LocalGitStore(repo)
            seed={p.relative_to(repo).as_posix():p.read_bytes() for p in out.rglob("*") if p.is_file()}
            with s.lock():
                s.publish(s.head(),seed,replace=set())
            def fetch(**kw):
                altered=rows()
                altered[0]["volume"]=11
                return altered
            actual=collect(out,state=adapter(repo,lambda symbol,**kw:fetch(**kw)))
            self.assertEqual(actual["status"],"ok")
            tip=s.head()
            path=PREFIX+f"series/daily/twelve_data_basic/{SYMBOL}/_index.json"
            prior=json.loads(seed[path])
            published=json.loads(s.read(tip,path))
            self.assertEqual(published["lineage_segments"][0],prior["lineage_segments"][0])
            self.assertIn("volume_conflict_quarantine_v1",{line["ingest_kind"] for line in published["lineage_segments"]})
            self.assertEqual(market_data_store.read_daily_series(out,provider="twelve_data_basic",symbol=SYMBOL)[:-1],sample)

    def test_lost_terminal_ack_is_unknown_until_exact_store_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)/"repo"
            out,_=initialize(repo)
            crossed=[]
            def fetch(symbol, **kw):
                crossed.append(symbol)
                return rows()
            state=adapter(repo,fetch)
            native_cas=state.store.cas
            def lose_after_terminal(ref,new,expected):
                native_cas(ref,new,expected)
                if state.store.exists(new,state.terminal):
                    raise OSError("synthetic lost durable terminal acknowledgement")
            with patch.object(state.store,"cas",side_effect=lose_after_terminal):
                first=collect(out,state=state)
            self.assertEqual(first["status"],"held_ordinary_eod_state_unknown")
            self.assertNotIn("ordinary_eod_local_store_commit",first)
            readback=LocalGitStore(repo)
            tip=readback.head()
            terminal=json.loads(readback.read(tip,state.terminal))
            self.assertEqual(terminal["state"],"COMPLETE")
            self.assertEqual(terminal["target_session"],SESSION)
            self.assertEqual(collect(out,state=adapter(repo,fetch))["status"],
                             "held_ordinary_eod_consumed_no_replay")
            self.assertEqual(readback.head(),tip)
            self.assertEqual(crossed,[SYMBOL])

    def test_existing_verified_target_is_enough_without_new_target_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)/"repo"
            out,_=initialize(repo)
            seen=[]
            # Create an internally consistent durable series before collection.
            market_data_store.append_daily_bars(out,provider="twelve_data_basic",symbol=SYMBOL,
                asset_class="stocks",series_semantics="daily_regular_ohlcv",
                adjustment_semantics="provider_reported",
                bars=[bar for bar in rows() if bar["trade_date"] <= SESSION])
            s=LocalGitStore(repo)
            seed={p.relative_to(repo).as_posix():p.read_bytes() for p in out.rglob("*") if p.is_file()}
            with s.lock():
                durable=s.publish(s.head(),seed,replace=set())
            def older(symbol, **kw):
                seen.append(symbol)
                return [bar for bar in rows() if bar["trade_date"] < SESSION]
            a=adapter(repo,older)
            first=collect(out,state=a)
            self.assertEqual(first["status"],"ok")
            tip=s.head()
            terminal=json.loads(s.read(tip,a.terminal))
            self.assertEqual(terminal["state"],"COMPLETE")
            self.assertEqual(terminal["target_session"],SESSION)
            self.assertEqual(terminal["targets"][SYMBOL]["source"],"ALREADY_DURABLE")
            self.assertEqual(terminal["targets"][SYMBOL]["index_sha256"],
               hashlib.sha256(s.read(durable,terminal["targets"][SYMBOL]["index_path"])).hexdigest())
            self.assertEqual(first["changed_series"],0)
            self.assertEqual(seen,[SYMBOL])

    def test_gate_rejects_early_and_exact_cutoff_without_claim(self):
        for clock in (lambda:START.replace(hour=6),lambda:STOP):
            with self.subTest(clock=clock), tempfile.TemporaryDirectory() as tmp:
                repo=Path(tmp)/"repo"
                out,origin=initialize(repo)
                crossed=[]
                def no(*a,**kw):
                    crossed.append(True)
                    raise AssertionError("early or expired route reached fetch")
                item=collect(out,state=adapter(repo,no,clock=clock))
                self.assertEqual(item["status"],"held_ordinary_eod_state_unknown")
                self.assertEqual(crossed,[])
                self.assertEqual(LocalGitStore(repo).head(),origin)


BUDGET=PREFIX+f"collector-state/{DAY[:7]}/{DAY}-twelve-data-budget.json"
if __name__=="__main__":
    unittest.main(verbosity=2)
