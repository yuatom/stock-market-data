"""Pure offline consistency checker. No trust, authorization or runtime resolver.
Input dictionaries must be constructed without lossy duplicate-key JSON parsing.
Artifact bytes are opaque: their digest does not prove tests actually ran.
"""
import hashlib
import re

PATHS = frozenset((
    'scripts/validate_deterministic_metrics.py',
    'config/calculation-policy.yaml',
    'tests/test_deterministic_metric_finalization.py',
))
KEYS = frozenset(('schema', 'provenance', 'expected_consumer_commit',
    'lineage_kind', 'files', 'verification_artifact_sha256'))

def _hex(value, length):
    return type(value) is str and re.fullmatch('[0-9a-f]{%d}' % length, value) is not None

def validate_manifest(manifest, files, artifact):
    """Return local consistency only. Never attest commit membership or deployment."""
    valid = (
        type(manifest) is dict and set(manifest) == KEYS
        and manifest['schema'] == 'consumer-candidate-offline-v1'
        and manifest['provenance'] == 'local-synthetic'
        and _hex(manifest['expected_consumer_commit'], 40)
        and manifest['lineage_kind'] == 'twelve_symbol_conditional_append'
        and type(manifest['files']) is dict and set(manifest['files']) == PATHS
        and type(files) is dict and set(files) == PATHS
        and type(artifact) is bytes and bool(artifact)
        and _hex(manifest['verification_artifact_sha256'], 64)
        and hashlib.sha256(artifact).hexdigest() == manifest['verification_artifact_sha256']
        and all(type(files[p]) is bytes and _hex(manifest['files'][p], 64)
                and hashlib.sha256(files[p]).hexdigest() == manifest['files'][p]
                for p in PATHS)
    )
    blockers = ['effective_runtime_resolver_not_implemented',
        'commit_tree_membership_not_verified', 'artifact_authenticity_not_verified']
    if not valid:
        blockers.append('local_manifest_inconsistent')
    return dict(local_manifest_consistent=bool(valid), production_ready=False,
        deployment_status='BLOCKED', verified_effective_consumer_commit=None,
        blockers=blockers)
