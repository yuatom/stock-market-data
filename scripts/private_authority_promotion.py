"""Dormant WRITE-capable authority promotion under the hosted collector lock.

Unlike the read-only materializer, this module can advance Store main once.
It is not authorization or a lock service. Only the independently approved
hosted job holding the actual repository-wide collector lock may invoke it.
No live action is authorized by this implementation or its synthetic tests.
"""
import base64
from datetime import datetime, timezone
import json
from zoneinfo import ZoneInfo
import os
from pathlib import Path
import re
import subprocess

from scripts.private_authority_input import ROUTES, STORE_REPOSITORY, _LocalGit, load_authority
from scripts.private_authority_preflight import _validate_identity
from scripts.private_authority_receipt import verify_receipt
from scripts.private_authority_receipt_transport import fetch_private_receipt
from scripts.private_authority_transport import resolve_private_reference

STORE_URL = 'https://github.com/'+STORE_REPOSITORY+'.git'
STOP = 'PRIVATE_AUTHORITY_PROMOTION_STOPPED_INSPECT_BEFORE_RETRY'


class PrivatePromotionError(RuntimeError):
    pass


def _need(value):
    if not value:
        raise ValueError()


def _utc_now():
    return datetime.now(timezone.utc)


def _fresh(selection):
    approval=json.loads(selection.approval_raw)
    now=_utc_now()
    _need(datetime.fromisoformat(approval['valid_from'])<=now<datetime.fromisoformat(approval['valid_until']))
    _need(now.astimezone(ZoneInfo('America/New_York')).date().isoformat()==approval['execution_day'])


def materialize_and_promote(*, token, route, compute_sha, authority_store_sha,
                            descriptor_commit, destination, source_recheck):
    """Validate receipt-bound objects, then one exact-head CAS and readback.

Pins must come from an independently authenticated receipt. Caller owns exact
execution approval and actual hosted concurrency, not an environment flag.
The mandatory callback rechecks reviewed Compute source before any promotion.
An unknown push/readback outcome stops; it never retries or rolls back.
"""
    try:
        _need(type(token) is str and re.fullmatch(r'[A-Za-z0-9_.-]{8,4096}',token))
        _need(type(route) is str and route in ROUTES and callable(source_recheck))
        for value in (compute_sha,authority_store_sha,descriptor_commit):
            _need(type(value) is str and re.fullmatch('[0-9a-f]{40}',value))
        target=Path(destination)
        _need(not os.path.lexists(target))
        parent=target.parent.resolve(strict=True)
        _need(parent.is_dir() and target.name not in ('','.','..'))
        target=parent/target.name
        _need(not os.path.lexists(target))
        ref=f'refs/heads/capture-authority/{route}/{compute_sha}'
        resolve_private_reference(token,route,compute_sha,descriptor_commit)
        target.mkdir(mode=0o700)
        env={k:v for k,v in os.environ.items() if k in {'PATH','HOME','TMPDIR','SYSTEMROOT'}}
        env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_SYSTEM='/dev/null',GIT_CONFIG_GLOBAL='/dev/null',
            GIT_NO_REPLACE_OBJECTS='1',GIT_NO_LAZY_FETCH='1',GIT_TERMINAL_PROMPT='0',LC_ALL='C')
        auth=dict(env,GIT_CONFIG_COUNT='1',GIT_CONFIG_KEY_0='http.'+STORE_URL+'.extraheader',
            GIT_CONFIG_VALUE_0='AUTHORIZATION: basic '+base64.b64encode(('x-access-token:'+token).encode()).decode())
        safety=['-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false',
            '-c','credential.helper=','-c','http.followRedirects=false','-c','http.proxy=',
            '-c','http.sslVerify=true','-c','protocol.allow=never','-c','protocol.https.allow=always']

        def git(*args,network=False):
            result=subprocess.run(['git','-C',str(target),*safety,*args],
                env=auth if network else env,capture_output=True,stdin=subprocess.DEVNULL,timeout=120)
            _need(result.returncode==0)
            return result.stdout.decode('ascii').strip()

        def remote_matches(main):
            observed=git('ls-remote','--refs','origin','refs/heads/main',ref,network=True)
            _need(sorted(observed.splitlines())==sorted([main+'\trefs/heads/main',descriptor_commit+'\t'+ref]))

        git('init','--quiet','--template=','--initial-branch=materialization')
        git('remote','add','origin',STORE_URL)
        git('fetch','--quiet','--no-tags','--no-recurse-submodules','origin',
            'refs/heads/main:refs/heads/main',ref+':'+ref,network=True)
        observed_main=git('rev-parse','--verify','refs/heads/main')
        _need(re.fullmatch('[0-9a-f]{40}',observed_main))
        _need(git('rev-parse','--verify',ref)==descriptor_commit)
        # Local-only ref preparation lets the existing strict selector validate
        # the exact immutable authority before a single remote write is allowed.
        git('update-ref','refs/heads/main',authority_store_sha,observed_main)
        git('checkout','--quiet','--detach',authority_store_sha)
        selection=load_authority(target,route,compute_sha,authority_store_sha,descriptor_commit)
        _,parents=_LocalGit(target).commit(authority_store_sha)
        _need(len(parents)==1)
        _need(observed_main in (parents[0],authority_store_sha))
        _fresh(selection)
        resolve_private_reference(token,route,compute_sha,descriptor_commit)
        remote_matches(observed_main)
        source_recheck()
        remote_matches(observed_main)
        _fresh(selection)
        if observed_main!=authority_store_sha:
            # No ancestry-based adoption, implicit lease, retry or repair.
            # Strict graph above proves this is a one-addition fast-forward.
            git('push','--quiet','--no-verify',
                '--force-with-lease=refs/heads/main:'+observed_main,
                'origin',authority_store_sha+':refs/heads/main',network=True)
        # Independently fetch advertised main and descriptor back, even when
        # main already held this authority. Never treat push acknowledgement as
        # sufficient. Unknown outcome exits without a second push/coordinator.
        remote_matches(authority_store_sha)
        git('fetch','--quiet','--no-tags','--no-recurse-submodules','origin',
            'refs/heads/main:refs/promotion-readback/main',
            ref+':refs/promotion-readback/descriptor',network=True)
        _need(git('rev-parse','--verify','refs/promotion-readback/main')==authority_store_sha)
        _need(git('rev-parse','--verify','refs/promotion-readback/descriptor')==descriptor_commit)
        resolve_private_reference(token,route,compute_sha,descriptor_commit)
        remote_matches(authority_store_sha)
        selected=load_authority(target,route,compute_sha,authority_store_sha,descriptor_commit)
        _need(selected.approval_raw==selection.approval_raw and selected.approval_sha256==selection.approval_sha256)
        _need(not git('status','--porcelain') and not git('ls-files','--others'))
        return selected
    except Exception:
        pass
    raise PrivatePromotionError(STOP)


def prepare_promoted_authority(*,token,route,compute_sha,expected_commitment,
                               destination,source_recheck,env=None):
    """Authenticate dispatch identity and independent receipt before write path.

This intentionally separate API does not change the read-only preflight APIs.
Caller must hold the actual hosted collector lock over this and the coordinator.
"""
    try:
        _validate_identity(route,compute_sha,env)
        _need(callable(source_recheck))
        raw=fetch_private_receipt(token,route,compute_sha,expected_commitment)
        receipt=verify_receipt(raw,expected_commitment,route,compute_sha)
        return materialize_and_promote(token=token,route=route,compute_sha=compute_sha,
            authority_store_sha=receipt.authority_store_sha,descriptor_commit=receipt.descriptor_commit,
            destination=destination,source_recheck=source_recheck)
    except Exception:
        pass
    raise PrivatePromotionError(STOP)
