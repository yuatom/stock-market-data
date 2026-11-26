"""Native wrapper and exact workflow shell-route tests; child processes controlled."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT=Path(__file__).resolve().parents[1]
WRAPPER=ROOT/"scripts/quiet_private_store.py"
WORKFLOW=ROOT/".github/workflows/market-data-collector-runtime.yml"
MANIFEST="collector-state/2001-01/2001-01-08-premarket-"+"a"*16+"-publication.json"
LINE=b"CAPTURE_AVAILABILITY_V2 "+b"a"*64+b"\n"
HOST=dict(GITHUB_ACTIONS="true",GITHUB_EVENT_NAME="push",GITHUB_REF="refs/heads/collector-requests",
          GITHUB_REPOSITORY="yuatom/stock-market-data",GITHUB_JOB="collect",GITHUB_SHA="1"*40,
          GITHUB_RUN_ID="123",GITHUB_RUN_ATTEMPT="1",MARKET_DATA_STORE_TOKEN="SyntheticToken")
SPEC=importlib.util.spec_from_file_location("quiet_candidate_for_test",WRAPPER)
WRAP=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(WRAP)


class Step17BridgeTest(unittest.TestCase):
    def drive(self, *, raw=LINE, err=b"", rc=0, host=None, origin=None, manifest=MANIFEST):
        cmds=[]
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        spool=Path(temp.name)/"capture-availability-v2-123-1-collect.txt"
        def launch(args,**kwargs):
            cmds.append(tuple(args))
            if "get-url" in args:
                return subprocess.CompletedProcess(args,0,(origin or WRAP.STORE_URL).encode()+b"\n",b"")
            if "rev-parse" in args:
                return subprocess.CompletedProcess(args,0,b"2"*40+b"\n",b"")
            kwargs["stdout"].write(raw)
            kwargs["stderr"].write(err)
            return subprocess.CompletedProcess(args,rc)
        buf=io.StringIO()
        with patch.dict(os.environ,{**HOST,"RUNNER_TEMP":temp.name,**(host or {})},clear=True), \
                patch.object(WRAP.subprocess,"run",side_effect=launch), contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()) as errbuf:
            result=WRAP.main(["verify-capture-availability","--repo",".","--manifest",manifest])
        self.assertEqual(errbuf.getvalue(),"")
        return result,buf.getvalue(),cmds, spool.read_bytes() if spool.exists() else None

    def test_dedicated_native_wrapper_egresses_one_exact_line_and_no_generic_child_stream(self):
        rc,output,cmds,spooled=self.drive()
        self.assertEqual((rc,output,spooled),(0,"CAPTURE_AVAILABILITY_ARTIFACT_READY\n",LINE))
        self.assertEqual(len(cmds),3)
        self.assertEqual(cmds[-1],
            (sys.executable,"-B",str(ROOT/"scripts/verify_store_publication.py"),
             "--store-root",".","--expected-commit","2"*40,"--writer-status","store_persisted",
             "--required-manifest","data/market-data/"+MANIFEST,"--emit-capture-availability-commitment"))

    def test_failure_unknown_and_noisy_output_is_never_forwarded(self):
        for raw,err,rc in [
            (LINE+b"PRIVATE SECRET\n",b"",0),(LINE+LINE,b"",0),(LINE.rstrip(),b"",0),
            (LINE,b"private line",0),(b"",b"",0),(b"Y"*8192,b"",0),(LINE,b"private failure",1)]:
            with self.subTest(raw=raw[:12],rc=rc):
                status,output,_,spooled=self.drive(raw=raw,err=err,rc=rc)
                self.assertIsNone(spooled)
                self.assertEqual(status,0 if not rc else 2)
                self.assertEqual(output,"PRIVATE_STORE_OPERATION_COMPLETE\n" if not rc else "PRIVATE_STORE_OPERATION_STOPPED\n")

    def test_wrong_context_origin_or_path_stops_before_child_verify(self):
        for kwargs in ({"host":{"GITHUB_EVENT_NAME":"schedule"}},
                       {"host":{"GITHUB_JOB":"other"}},{"origin":"https://example.invalid"},
                       {"manifest":"--bad"},{"manifest":MANIFEST+"\nPRIVATE"}):
            with self.subTest(kwargs=kwargs):
                rc,out,cmds,spooled=self.drive(**kwargs)
                self.assertIsNone(spooled)
                self.assertEqual((rc,out),(2,"PRIVATE_STORE_OPERATION_STOPPED\n"))
                self.assertFalse(any("verify_store_publication.py" in " ".join(cmd) for cmd in cmds))

    def test_exact_shell_branch_default_and_nonpremarket_use_original_quiet_run(self):
        step=next(x for x in yaml.safe_load(WORKFLOW.read_bytes())["jobs"]["collect"]["steps"]
                  if x.get("name")=="Verify durable Store publication")
        self.assertEqual(step["env"]["PRE_CAPTURE_AVAILABILITY_ENABLED"],"false")
        shell=step["run"].replace("${{ steps.publication.outputs.publication_manifest_path }}",MANIFEST)
        for flag,mode,event,want in [("","premarket","push","run"),("true","open_30m","push","run"),
                                  ("true","premarket","schedule","run"),
                                  ("true","premarket","push","PRIVATE_STORE_OPERATION_COMPLETE")]:
            with tempfile.TemporaryDirectory() as temp:
                launcher=Path(temp)/"python"; launcher.write_text('#!/bin/sh\nprintf "%s\\n" "$3"\n')
                launcher.chmod(0o700)
                env={**os.environ,**HOST,"PATH":f"{temp}:/usr/bin:/bin",
                     "PRE_CAPTURE_AVAILABILITY_ENABLED":flag,"PRE_CAPTURE_AVAILABILITY_MODE":mode,
                     "GITHUB_EVENT_NAME":event}
                result=subprocess.run(["/bin/bash","-euo","pipefail","-c",shell],
                                      env=env,capture_output=True,text=True,timeout=10)
                self.assertEqual((result.returncode,result.stdout,result.stderr),(0,want+"\n",""))


    def test_certificate_uploader_records_actual_failure_and_is_ready_gated(self):
        steps=yaml.safe_load(WORKFLOW.read_bytes())["jobs"]["collect"]["steps"]
        idx=next(i for i,s in enumerate(steps) if s.get("name")=="Verify durable Store publication")
        verify, uploader=steps[idx:idx+2]
        self.assertEqual(verify["env"]["PRE_CAPTURE_AVAILABILITY_ENABLED"],"false")
        self.assertEqual(verify["id"],"verify_durable_store")
        self.assertEqual(uploader["name"],"Upload capture availability commitment")
        self.assertEqual(uploader["if"],
            "${{ success() && steps.verify_durable_store.outputs.capture_availability_artifact_ready == 'true' }}")
        self.assertIs(uploader.get("continue-on-error",False),False,
            "Actions REST conclusion must not mask a failed certificate upload")
        self.assertEqual(uploader["with"]["if-no-files-found"],"error")

    def test_pinned_single_file_action_uses_no_zip_no_overwrite(self):
        steps=yaml.safe_load(WORKFLOW.read_bytes())["jobs"]["collect"]["steps"]
        idx=next(i for i,s in enumerate(steps) if s.get("name")=="Verify durable Store publication")
        uploader=steps[idx+1]
        self.assertEqual(uploader["uses"],"actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a")
        self.assertEqual(uploader["with"]["path"],
            "${{ runner.temp }}/capture-availability-v2-${{ github.run_id }}-${{ github.run_attempt }}-collect.txt")
        self.assertIs(uploader["with"]["archive"],False)
        self.assertIs(uploader["with"]["overwrite"],False)
        self.assertEqual(uploader["with"]["retention-days"],3)

    def test_spool_is_exclusive_and_never_reuses_existing_symlink_or_stale_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            expected=root/"capture-availability-v2-123-1-collect.txt"
            env={**HOST,"RUNNER_TEMP":temp}
            other=root/"private"
            other.write_bytes(b"PRIVATE FIXTURE")
            expected.symlink_to(other)
            self.assertFalse(WRAP.spool_capture_availability(LINE,env))
            self.assertEqual(other.read_bytes(),b"PRIVATE FIXTURE")
            expected.unlink()
            self.assertTrue(WRAP.spool_capture_availability(LINE,env))
            self.assertFalse(WRAP.spool_capture_availability(LINE.replace(b"a"*64,b"b"*64),env))
            self.assertEqual(expected.read_bytes(),LINE)
            self.assertEqual(expected.stat().st_mode & 0o777,0o600)

    def test_exact_shell_only_sets_ready_for_native_boolean_not_a_proof_like_line(self):
        step=next(x for x in yaml.safe_load(WORKFLOW.read_bytes())["jobs"]["collect"]["steps"]
                  if x.get("name")=="Verify durable Store publication")
        shell=step["run"].replace("${{ steps.publication.outputs.publication_manifest_path }}",MANIFEST)
        for raw in ("CAPTURE_AVAILABILITY_ARTIFACT_READY","CAPTURE_AVAILABILITY_ARTIFACT_READY extra",
                    LINE.decode().strip(),"private synthetic body"):
            with tempfile.TemporaryDirectory() as temp:
                root=Path(temp)
                launcher=root/"python"; launcher.write_text('#!/bin/sh\nprintf "%s\\n" "$FIXTURE_OUTPUT"\n')
                launcher.chmod(0o700)
                output=root/"outputs"
                env={**os.environ,**HOST,"PATH":f"{temp}:/usr/bin:/bin",
                     "PRE_CAPTURE_AVAILABILITY_ENABLED":"true","PRE_CAPTURE_AVAILABILITY_MODE":"premarket",
                     "FIXTURE_OUTPUT":raw,"GITHUB_OUTPUT":str(output)}
                result=subprocess.run(["/bin/bash","-euo","pipefail","-c",shell],env=env,
                                      capture_output=True,text=True,timeout=10)
                self.assertEqual((result.returncode,result.stdout,result.stderr),(0,"PRIVATE_STORE_OPERATION_COMPLETE\n",""))
                self.assertEqual(output.read_bytes() if output.exists() else None,
                    b"capture_availability_artifact_ready=true\n"
                    if raw=="CAPTURE_AVAILABILITY_ARTIFACT_READY" else None)

    def test_default_shell_still_passes_exact_native_arguments(self):
        step=next(x for x in yaml.safe_load(WORKFLOW.read_bytes())["jobs"]["collect"]["steps"]
                  if x.get("name")=="Verify durable Store publication")
        shell=step["run"].replace("${{ steps.publication.outputs.publication_manifest_path }}",MANIFEST)
        with tempfile.TemporaryDirectory() as temp:
            fake=Path(temp)
            (fake/"git").write_text('#!/bin/sh\nprintf "%s\\n" "'+"2"*40+'"\n')
            (fake/"python").write_text("""#!/bin/sh
if [ "$3" = run ]; then exec /bin/bash -euo pipefail; fi
printf '%s\n' "$@"
""")
            for p in fake.iterdir():
                p.chmod(0o700)
            env={**os.environ,**HOST,"PATH":f"{temp}:/usr/bin:/bin",
                 "PRE_CAPTURE_AVAILABILITY_ENABLED":"false","PRE_CAPTURE_AVAILABILITY_MODE":"premarket",
                 "GITHUB_OUTPUT":str(fake/"outputs")}
            result=subprocess.run(["/bin/bash","-euo","pipefail","-c",shell],env=env,
                                  capture_output=True,text=True,timeout=10)
            self.assertEqual((result.returncode,result.stderr),(0,""))
            self.assertFalse((fake/"outputs").exists())
            self.assertEqual(result.stdout.splitlines(),
                ["../compute/scripts/verify_store_publication.py","--store-root",".","--expected-commit","2"*40,
                 "--writer-status","store_persisted","--required-manifest","data/market-data/"+MANIFEST])


if __name__=="__main__":
    unittest.main()
