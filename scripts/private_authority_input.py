"""Read a pinned PRIVATE authority selection from local Git objects, without I/O writes.

This module does not authorize an operation, create a private approval/ref, fetch
objects, change checkout, call providers, or activate either deployment workflow.
A separately verified private receipt must supply descriptor_commit. Ref/file
presence is not consent. The caller must independently establish explicit user
permission, remote privacy/head/ref immutability, freshness, budget, lock ownership
and absence of prior/uncertain execution, then use the unchanged runtime gates.

Only normal, complete SHA-1 working repositories are supported. Git executable,
OS and repository storage must be trusted; concurrent mutation is not supported.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from scripts import eleven_symbol_append as append
from scripts.raw_response_evidence import validate_request

DESCRIPTOR_PATH = 'capture-authority/selection.json'
ROUTES = frozenset({'soxx_capture_only', 'eleven_symbol_conditional_append'})
MAX_JSON_BYTES = 65536
STORE_REPOSITORY = 'yuatom/stock-market-data-store'
DESCRIPTOR_KEYS = frozenset({'schema_version', 'kind', 'route', 'compute_sha',
                            'authority_store_sha', 'approval_sha256', 'approval'})
COMMON_APPROVAL_KEYS = frozenset({'schema_version', 'mode', 'compute_sha', 'execution_day',
    'valid_from', 'valid_until', 'request_limit', 'credit_limit', 'evidence_repository', 'evidence_prefix'})
SOXX_KEYS = COMMON_APPROVAL_KEYS | {'baseline_store_sha', 'request', 'evidence_repository_id', 'market_fact_write'}
ELEVEN_KEYS = COMMON_APPROVAL_KEYS | {'expected_store_sha', 'private_contract_sha256', 'registry_sha256',
                                    'historical_cutoff', 'basis_policy'}
ENV_OVERRIDES = frozenset({'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY',
    'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_SHALLOW_FILE', 'GIT_GRAFT_FILE', 'GIT_REPLACE_REF_BASE',
    'GIT_CONFIG', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS', 'GIT_INDEX_FILE'})


class PrivateAuthorityError(RuntimeError):
    """A value-free failure: never print underlying Git errors or private objects."""


@dataclass(frozen=True, repr=False)
class PrivateAuthoritySelection:
    """Private in-process values only, never public logs, argv or workflow outputs."""
    approval_raw: bytes = field(repr=False)
    approval_sha256: str = field(repr=False)
    authority_store_sha: str = field(repr=False)
    descriptor_commit: str = field(repr=False)

    def __repr__(self):
        return '<PrivateAuthoritySelection>'

    @property
    def authorization_authenticated(self):
        return False

    @property
    def production_activation(self):
        return False


def _need(condition):
    if not condition:
        raise ValueError()


def _hex(value, size=40):
    _need(type(value) is str and re.fullmatch('[0-9a-f]{' + str(size) + '}', value))
    return value


def _encode(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False) + '\n').encode('ascii')


def _json(raw):
    _need(type(raw) is bytes and 0 < len(raw) <= MAX_JSON_BYTES)

    def pairs(items):
        result = {}
        for key, value in items:
            _need(key not in result)
            result[key] = value
        return result

    def constant(_):
        raise ValueError()

    value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    _need(_encode(value) == raw)
    return value


def _day(value):
    _need(type(value) is str and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value))
    _need(date.fromisoformat(value).isoformat() == value)
    return value


def _window(approval):
    stamps = []
    for name in ('valid_from', 'valid_until'):
        value = approval[name]
        _need(type(value) is str and re.fullmatch(
            r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)', value))
        stamps.append(datetime.fromisoformat(value))
    _need(stamps[0] < stamps[1])
    _day(approval['execution_day'])
    # Freshness and ET accounting-day checks remain mandatory in the runtime.


class _LocalGit:
    """Bounded literal Git plumbing only. No remote, checkout, status or writes."""
    def __init__(self, store):
        _need(not (ENV_OVERRIDES & os.environ.keys()))
        _need(not any(name.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_')) for name in os.environ))
        # Allow only these safe ambient suppressors used by offline test harnesses.
        for name, allowed in {'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null',
                              'GIT_CONFIG_NOSYSTEM': '1'}.items():
            _need(name not in os.environ or os.environ[name] == allowed)
        self.repo = Path(store).resolve(strict=True)
        self.gitdir = self.repo / '.git'
        _need(self.repo.is_dir() and self.gitdir.is_dir() and not self.gitdir.is_symlink())
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        self.env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_SYSTEM='/dev/null', GIT_CONFIG_GLOBAL='/dev/null',
            GIT_NO_REPLACE_OBJECTS='1', GIT_NO_LAZY_FETCH='1', GIT_OPTIONAL_LOCKS='0',
            GIT_TERMINAL_PROMPT='0', GIT_ALLOW_PROTOCOL='', LC_ALL='C')
        self.assert_unmodified_graph()
        _need(self.text('rev-parse', '--show-object-format') == 'sha1')
        _need(self.text('rev-parse', '--is-bare-repository') == 'false')
        # No local configuration includes or promisor object acquisition.
        config = self.read('config', '--local', '--no-includes', '--list', '-z')
        for record in config.split(b'\0'):
            name = record.split(b'\n', 1)[0].lower()
            _need(not (name.startswith((b'include.', b'includeif.')) or name == b'extensions.partialclone'
                       or (name.startswith(b'remote.') and name.endswith(b'.promisor'))))

    def read(self, *args):
        result = subprocess.run(['git', '-C', str(self.repo), '--no-replace-objects',
            '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
            '-c', 'core.commitGraph=false', '-c', 'core.useReplaceRefs=false',
            '-c', 'protocol.allow=never', *args], env=self.env, capture_output=True, timeout=30,
            stdin=subprocess.DEVNULL)
        _need(result.returncode == 0)
        return result.stdout

    def text(self, *args):
        return self.read(*args).decode('ascii').rstrip('\n')

    def assert_unmodified_graph(self):
        for relative in ('info/grafts', 'shallow', 'commondir', 'objects/info/alternates', 'objects/info/http-alternates'):
            _need(not os.path.lexists(self.gitdir / relative))
        for relative in ('objects', 'objects/info', 'info', 'refs'):
            _need(not (self.gitdir / relative).is_symlink())
        _need(not self.read('for-each-ref', '--format=%(refname)', 'refs/replace/'))

    def object(self, oid, kind, bound):
        _hex(oid)
        _need(self.text('cat-file', '-t', oid) == kind)
        size = self.text('cat-file', '-s', oid)
        _need(size.isascii() and size.isdecimal() and 0 < int(size) <= bound)
        raw = self.read('cat-file', kind, oid)
        _need(len(raw) == int(size))
        _need(hashlib.sha1(kind.encode() + b' ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() == oid)
        return raw

    def commit(self, oid):
        raw = self.object(oid, 'commit', MAX_JSON_BYTES)
        header, separator, _ = raw.partition(b'\n\n')
        _need(separator)
        lines = header.split(b'\n')
        _need(lines and lines[0].startswith(b'tree '))
        trees = [line[5:].decode('ascii') for line in lines if line.startswith(b'tree ')]
        parents = [line[7:].decode('ascii') for line in lines if line.startswith(b'parent ')]
        _need(len(trees) == 1)
        for identity in trees + parents:
            _hex(identity)
        self.object(trees[0], 'tree', 4 * 1024 * 1024)
        return trees[0], tuple(parents)

    def normal_blob_identity(self, commit, relative):
        # Paths are only fixed code constants or validated hashes/closed filenames.
        _need(type(relative) is str and re.fullmatch(r'[a-zA-Z0-9_./-]+', relative)
              and not relative.startswith('/') and all(p not in ('', '.', '..') for p in relative.split('/')))
        tree, _ = self.commit(commit)
        parts = relative.split('/')
        for position, component in enumerate(parts):
            self.object(tree, 'tree', 4 * 1024 * 1024)
            record = self.read('ls-tree', '-z', tree, '--', component)
            last = position == len(parts) - 1
            prefix = b'100644 blob ' if last else b'040000 tree '
            match = re.fullmatch(prefix + rb'([0-9a-f]{40})\t' + re.escape(component.encode()) + b'\0', record)
            _need(match)
            tree = match.group(1).decode('ascii')
        return tree

    def blob(self, commit, relative):
        oid = self.normal_blob_identity(commit, relative)
        return oid, self.object(oid, 'blob', MAX_JSON_BYTES)

    def single_addition(self, commit, parent, relative, blob):
        _, parents = self.commit(commit)
        _need(parents == (parent,))
        self.commit(parent)
        expected = (':000000 100644 ' + '0' * 40 + ' ' + blob + ' A\0' + relative + '\0').encode('ascii')
        delta = self.read('diff-tree', '--no-commit-id', '--no-ext-diff', '--no-textconv',
                          '--no-renames', '--raw', '--no-abbrev', '-r', '-z', parent, commit, '--')
        _need(delta == expected)

    def bind_refs(self, route, compute, head, descriptor):
        ref = f'refs/heads/capture-authority/{route}/{compute}'
        _need(self.read('for-each-ref', '--format=%(refname) %(objectname) %(objecttype) %(symref)', ref)
              == (ref + ' ' + descriptor + ' commit \n').encode('ascii'))
        _need(self.text('rev-parse', '--verify', 'HEAD') == head)
        _need(self.read('for-each-ref', '--format=%(refname) %(objectname) %(objecttype) %(symref)', 'refs/heads/main')
              == ('refs/heads/main ' + head + ' commit \n').encode('ascii'))


def _approval_common(value, route, compute):
    _need(type(value) is dict and set(value) == (SOXX_KEYS if route == 'soxx_capture_only' else ELEVEN_KEYS))
    _need(type(value['schema_version']) is int and value['schema_version'] == 1)
    _need(value['mode'] == route and value['compute_sha'] == compute)
    _need(value['evidence_repository'] == STORE_REPOSITORY and value['evidence_prefix'] == 'capture-evidence')
    for name in ('request_limit', 'credit_limit'):
        _need(type(value[name]) is int and value[name] == (1 if route == 'soxx_capture_only' else 11))
    _window(value)


def _soxx(reader, descriptor, head, compute):
    _need(descriptor['approval'] is None)
    relative = 'capture-approvals/' + descriptor['approval_sha256'] + '.json'
    blob, raw = reader.blob(head, relative)
    _need(hashlib.sha256(raw).hexdigest() == descriptor['approval_sha256'])
    approval = _json(raw)
    _approval_common(approval, 'soxx_capture_only', compute)
    baseline = _hex(approval['baseline_store_sha'])
    _need(approval['market_fact_write'] is False)
    _need(type(approval['evidence_repository_id']) is int and approval['evidence_repository_id'] > 0)
    request = validate_request(approval['request'])
    _need(request['symbol'] == 'SOXX' and request['mic_code'] == 'XNMS'
          and request['end_date'] < approval['execution_day'])
    reader.single_addition(head, baseline, relative, blob)
    return raw


def _eleven(reader, descriptor, head, compute):
    approval = descriptor['approval']
    _approval_common(approval, 'eleven_symbol_conditional_append', compute)
    _need(approval['expected_store_sha'] == head)
    digest = _hex(approval['private_contract_sha256'], 64)
    _hex(approval['registry_sha256'], 64)
    _need(approval['basis_policy'] == 'full_ohlc_overlap_preserve_volume_v1')
    cutoff = _day(approval['historical_cutoff'])
    _need(cutoff < approval['execution_day'])
    raw = _encode(approval)
    _need(hashlib.sha256(raw).hexdigest() == descriptor['approval_sha256'])
    relative = 'capture-contracts/' + digest + '.json'
    blob, contract_raw = reader.blob(head, relative)
    _need(hashlib.sha256(contract_raw).hexdigest() == digest)
    contract = _json(contract_raw)
    _need(type(contract) is dict and set(contract) == {'schema_version', 'kind', 'baseline_store_sha', 'historical_cutoff', 'targets'})
    _need(type(contract['schema_version']) is int and contract['schema_version'] == 1
          and contract['kind'] == 'eleven_symbol_append_contract' and contract['historical_cutoff'] == cutoff)
    baseline = _hex(contract['baseline_store_sha'])
    reader.single_addition(head, baseline, relative, blob)
    targets = contract['targets']
    _need(type(targets) is list and tuple(x.get('symbol') for x in targets) == append.SYMBOLS)
    for target in targets:
        append.validate_scope(target, target['symbol'])
        _need(target['addition_dates'][-1] == cutoff)
        for name, expected in target['files'].items():
            relative = append.series_prefix(target['symbol']) + name
            actual = reader.normal_blob_identity(baseline, relative)
            _need(actual == expected)
    # Existing runtime still validates registry, full baseline closure/rows,
    # overlap and freshness before any debit/request or market publication.
    return raw


def load_authority(store, route, compute_sha, expected_store_head, descriptor_commit):
    """Select only a privately pinned local object, never permission or execution.

The exact private descriptor commit must come from an independently verified
receipt, not an untrusted ref lookup. Its local ref binding is checked without
fetching or moving it. Callers must keep the returned private values off public
logs, argv, environment dumps, dispatch inputs and outputs. The Store checkout
remains at expected_store_head, not at the off-main descriptor commit.
"""
    try:
        _need(type(route) is str and route in ROUTES)
        for identity in (compute_sha, expected_store_head, descriptor_commit):
            _hex(identity)
        reader = _LocalGit(store)
        reader.bind_refs(route, compute_sha, expected_store_head, descriptor_commit)
        blob, raw = reader.blob(descriptor_commit, DESCRIPTOR_PATH)
        descriptor = _json(raw)
        _need(type(descriptor) is dict and set(descriptor) == DESCRIPTOR_KEYS)
        _need(type(descriptor['schema_version']) is int and descriptor['schema_version'] == 1
              and descriptor['kind'] == 'capture_authority_selection' and descriptor['route'] == route
              and descriptor['compute_sha'] == compute_sha and descriptor['authority_store_sha'] == expected_store_head)
        _hex(descriptor['approval_sha256'], 64)
        reader.single_addition(descriptor_commit, expected_store_head, DESCRIPTOR_PATH, blob)
        approval_raw = (_soxx if route == 'soxx_capture_only' else _eleven)(reader, descriptor, expected_store_head, compute_sha)
        reader.assert_unmodified_graph()
        reader.bind_refs(route, compute_sha, expected_store_head, descriptor_commit)
        return PrivateAuthoritySelection(approval_raw, descriptor['approval_sha256'], expected_store_head, descriptor_commit)
    except Exception:
        pass
    # Outside the except block, even exception context contains no private error.
    raise PrivateAuthorityError('PRIVATE_AUTHORITY_INVALID')
