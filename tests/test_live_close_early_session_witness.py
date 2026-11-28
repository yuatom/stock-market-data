"""Current legacy Close wiring: executable negative, not an approved calendar adapter."""
from __future__ import annotations

import contextlib
import io
import json
import os
from datetime import datetime
import yaml
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
import collect_market_data as entry
import collection_universe as universe_owner
import market_data_collection as runtime
import build_deterministic_metric_proof as proof

FRIDAY="2026-11-27"
NORMAL="2026-11-30"
# Given independent synthetic session facts for the expectation only. The actual
# legacy collector has no admitted calendar input that can receive these facts.
EXPECTED_END_WINDOW={FRIDAY:("12:45","13:00"),NORMAL:("15:45","16:00")}

def args(root,mode,day):
    return ["--mode",mode,"--trade-date",day,"--store-root",str(root),
            "--universe",str(ROOT/"config/collection-universe.json"),
            "--config",str(ROOT/"config/market-data-store.yaml"),
            "--access-config",str(ROOT/"config/market-data-collector-access.yaml")]

class LiveCloseEarlySessionWitness(unittest.TestCase):
    def seed(self,root,day):
        universe=universe_owner.load_collection_universe(ROOT/"config/collection-universe.json")
        eligible=entry._live_close_universe(universe)
        start,end=EXPECTED_END_WINDOW[day]
        stamp=f"{day}T{start}:00-05:00"
        generated=f"{day}T{end}:05-05:00"
        refs=[]
        for symbol,asset in eligible:
            row=universe_owner.decorate_fact(universe,dict(symbol=symbol,asset_class=asset,session="regular",
                event_time=stamp,source_timestamp=stamp,last_sale=10.0,reported_volume=1.0,source_contract="nasdaq_public_chart_last_sale_volume_v1"))
            rel,blob=runtime.write_capture(root,trade_date=day,session="regular",provider=runtime.NASDAQ,
                capture_id="synthetic-"+symbol.lower(),generated_at=generated,actual_data_cutoff=stamp,
                window={"start":start,"end":end},feed_scope="nasdaq_public_chart_last_sale_volume_v1",
                qualified_facts=[row],missing_symbols=[])
            refs.append(dict(path=rel,blob_sha=blob,kind="regular_intraday_capture",
                             window=f"{start}-{end}",provider=runtime.NASDAQ,symbol=symbol))
        runtime.write_snapshot(root,stage="close",trade_date=day,snapshot_id="synthetic-close",
            generated_at=generated,data_refs=refs,coverage={},missing=[],
            target_window={"start":start,"end":end},actual_data_cutoff=stamp)
        self.assertEqual(entry.base._load_prior_snapshot(root,day,"close")[0],refs)
        return [s for s,_ in eligible]

    def before_provider(self,root,mode,day):
        # No actual collection, metering, HTTP or proof writer invoked.
        with (patch.object(runtime,"collect_regular_window",return_value={}) as called,
              patch("socket.getaddrinfo",side_effect=AssertionError("network forbidden")),
              contextlib.redirect_stdout(io.StringIO()) as stdout):
            self.assertEqual(entry._run_live_close(args(root,mode,day)),0)
        return called,json.loads(stdout.getvalue())

    def test_primary_sends_official_early_close_window(self):
        with tempfile.TemporaryDirectory() as name:
            called,_=self.before_provider(Path(name),"close",FRIDAY)
        self.assertTrue(called.called)
        kw=called.call_args.kwargs
        self.assertEqual((kw["start_et"],kw["end_et"]),EXPECTED_END_WINDOW[FRIDAY])

    def test_existing_exact_captures_are_reused_for_normal_and_early_close(self):
        for day in (FRIDAY,NORMAL):
            with self.subTest(day=day), tempfile.TemporaryDirectory() as name:
                root=Path(name)
                symbols=self.seed(root,day)
                before={p.relative_to(root):p.read_bytes() for p in root.rglob("*") if p.is_file()}
                for mode in ("close_retry","close_final"):
                    called,status=self.before_provider(root,mode,day)
                    self.assertFalse(called.called)
                    self.assertEqual(status["status"],"partial_window_no_supplement_authority")
                    self.assertEqual(status["retained_close_partial_count"],len(symbols))
                    self.assertEqual(status["retained_close_empty_count"],0)
                self.assertEqual({p.relative_to(root):p.read_bytes() for p in root.rglob("*") if p.is_file()},before)

    def test_metric_proof_uses_calendar_not_snapshot_assertion(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            symbols=self.seed(root,FRIDAY)
            snapshot,_,_=proof._load_snapshot(root,FRIDAY,"close")
            self.assertEqual(set(proof._target_observations(root,snapshot)),set(symbols))
            snapshot["target_window"]={"start":"15:45","end":"16:00"}
            with self.assertRaises(proof.MetricProofError):
                proof._target_observations(root,snapshot)

    def test_existing_push_admission_keeps_early_date_and_pins(self):
        path=ROOT/".github/workflows/market-data-collector-runtime.yml"
        workflow=yaml.safe_load(path.read_text())
        body,=[step["run"] for job in workflow["jobs"].values() for step in job["steps"]
               if step.get("name")=="Resolve and validate collector invocation"]
        class FixedDateTime(datetime):
            @classmethod
            def now(cls,tz=None):
                return cls.fromisoformat(FRIDAY+"T13:06:00-05:00").astimezone(tz)
        body=body.replace("from datetime import datetime","datetime = clock",1)
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            request=root/"control/requests/collector-request.json"
            request.parent.mkdir(parents=True)
            request.write_text(json.dumps(dict(schema_version=1,request_id="synthetic-early",
                requested_at=FRIDAY+"T13:06:00-05:00",trade_date=FRIDAY,stage="close",mode="close",
                research_repository="yuatom/stock-dairy",research_repository_commit_sha="2"*40,
                market_data_contract_sha="1"*40,transaction_id="synthetic",
                personal_state_proof=dict(dynamic_state_read_sha="3"*40,blob_sha="4"*40,state_version=1,
                    authority_state="canonical",content_hash_status="unavailable_no_script",content_sha256=None),
                research_universe_resolution_status="resolved")))
            env=dict(EVENT_NAME="push",EVENT_BRANCH="collector-requests",CONTROL_REF="5"*40,
                     RUNNER_TEMP=name,GITHUB_OUTPUT=str(root/"out"))
            real_cwd=os.getcwd()
            try:
                os.chdir(name)
                with patch.dict(os.environ,env), patch("subprocess.check_output",side_effect=["1"*40,"6"*40]), contextlib.redirect_stdout(io.StringIO()):
                    exec(compile(body,str(path),"exec"),{"clock":FixedDateTime})
            finally:
                os.chdir(real_cwd)
            self.assertIn("mode=close\ntrade_date="+FRIDAY+"\n",(root/"out").read_text())
            self.assertEqual((root/"market-data-store-input-sha").read_text(),"6"*40+"\n")

    def test_no_provider_before_verified_early_cutoff(self):
        def wait(hm):
            now=datetime.fromisoformat(FRIDAY+"T"+hm+":00-05:00")
            return entry.regular_stage_cutoff_wait_seconds(mode="close",trade_date=FRIDAY,now_et=now)
        with self.assertRaises(SystemExit):
            wait("12:40")
        self.assertEqual(wait("12:58"),120)
        self.assertEqual(wait("13:00"),0)

if __name__=="__main__":unittest.main()
