"""Unbound hosted identity + materialization preflight. Not execution authority.

The env argument is trusted platform context in a future reviewed workflow,
not an attestation API. Caller must authenticate the private creation receipt,
verify exact reviewed source checkout/remote main and hold the same-repository
collector concurrency lock before using this preflight. This module deliberately
has no CLI, provider key, execution coordinator or workflow entrypoint.
"""
import os
import re
from scripts.private_authority_materialize import materialize_store
from scripts.private_authority_receipt_transport import fetch_private_receipt

WORKFLOWS = {'soxx_capture_only': 'soxx-capture-only.yml',
             'eleven_symbol_conditional_append': 'eleven-symbol-append.yml',
             'twelve_symbol_conditional_append': 'twelve-symbol-append.yml'}


class PrivatePreflightError(RuntimeError):
    pass


def _validate_identity(route, compute_sha, env):
    env = os.environ if env is None else env
    if type(route) is not str or route not in WORKFLOWS:
        raise ValueError()
    if type(compute_sha) is not str or not re.fullmatch('[0-9a-f]{40}', compute_sha):
        raise ValueError()
    expected = dict(GITHUB_ACTIONS='true',GITHUB_EVENT_NAME='workflow_dispatch',
        GITHUB_REPOSITORY='yuatom/stock-market-data',GITHUB_REF='refs/heads/main',
        GITHUB_SHA=compute_sha,GITHUB_RUN_ATTEMPT='1',GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom',
        GITHUB_WORKFLOW_REF='yuatom/stock-market-data/.github/workflows/'+WORKFLOWS[route]+'@refs/heads/main')
    if not all(env.get(k) == v for k,v in expected.items()):
        raise ValueError()
    if not re.fullmatch('[1-9][0-9]*', env.get('GITHUB_RUN_ID','')):
        raise ValueError()


def prepare_private_authority(*, token, route, compute_sha, expected_store_head,
                              descriptor_commit, destination, env=None):
    """Reject unsupported invocation before any private access; return selection.

A return value remains authorization_authenticated=False and
production_activation=False. Never treat preflight success as user consent.
"""
    try:
        _validate_identity(route, compute_sha, env)
        return materialize_store(token,route,compute_sha,expected_store_head,descriptor_commit,destination)
    except Exception:
        pass
    raise PrivatePreflightError('PRIVATE_PREFLIGHT_INVALID')


def prepare_committed_authority(*, token, route, compute_sha, receipt_raw,
                                expected_commitment, destination, env=None):
    """LOCAL CANDIDATE: bind private receipt before materialization.

Expected commitment must come from the separately approved prospective dispatch
interface, never the fetched receipt. Publishing that opaque value/interface is
not authorized by this code. This function neither fetches the sidecar nor
executes providers; all trusted-caller/source/lock obligations above remain.
"""
    try:
        from scripts.private_authority_receipt import verify_receipt
        receipt = verify_receipt(receipt_raw, expected_commitment, route, compute_sha)
        return prepare_private_authority(token=token,route=route,compute_sha=compute_sha,
            expected_store_head=receipt.authority_store_sha,descriptor_commit=receipt.descriptor_commit,
            destination=destination,env=env)
    except Exception:
        pass
    raise PrivatePreflightError('PRIVATE_PREFLIGHT_INVALID')


def prepare_hosted_authority(*, token, route, compute_sha, expected_commitment, destination, env=None):
    """Unbound quiet sidecar + identity + commitment + materialization pipeline.

The reviewed hosted caller still owns exact source/concurrency and one-shot
execution approval. This pipeline returns private selection only; it has no
provider key, runtime execution, dispatch or authorization creation capability.
"""
    try:
        _validate_identity(route, compute_sha, env)
        raw = fetch_private_receipt(token, route, compute_sha, expected_commitment)
        return prepare_committed_authority(token=token,route=route,compute_sha=compute_sha,
            receipt_raw=raw,expected_commitment=expected_commitment,destination=destination,env=env)
    except Exception:
        pass
    raise PrivatePreflightError('PRIVATE_PREFLIGHT_INVALID')
