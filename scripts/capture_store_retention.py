"""Create-only isolated Store evidence. No workflow, CLI or provider operations.

Caller must independently verify current destination repository is private and
approved for these bytes, hold the official shared lock, and chain returned exact
commit identities. No market inventory/manifests/claims are modified here.
"""
import hashlib
import os
import re
from pathlib import Path

import admit_market_data_baseline as admission
import verify_store_publication as publication

LIMITS={'claim.json':65536,'request.json':65536,'response.raw':4*1024*1024,
        'envelope.json':12*1024*1024,'completion.json':65536,
        'attempt.json':65536,'terminal.json':65536}

GIT_SAFETY=('-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false')

def _git(repo,*args):
    return admission.git(repo,*GIT_SAFETY,*args)

class _CaptureTransport(publication._PublicationTransport):
    def once(self,*args):
        return super().once(*GIT_SAFETY,*args)

class _EvidenceWriter:
    def __init__(self,repo,expected,day,request_id):
        self.repo=Path(repo).resolve()
        self.base=expected
        self.allowed=set()
        if not re.fullmatch('[0-9a-f]{40}',expected):raise ValueError()
        if not re.fullmatch('[0-9]{4}-[0-9]{2}-[0-9]{2}',day):raise ValueError()
        if not re.fullmatch('[0-9]+-[0-9]+',request_id):raise ValueError()
        if _git(self.repo,'rev-parse','HEAD')!=expected or self.remote_head()!=expected:raise ValueError()
        if _git(self.repo,'status','--porcelain') or _git(self.repo,'ls-files','--others'):raise ValueError()

    def remote_head(self):
        parts=_git(self.repo,'ls-remote','origin','refs/heads/main').split()
        if len(parts)!=2 or parts[1]!='refs/heads/main':raise ValueError()
        return parts[0]

    def safe_path(self,relative):
        if relative not in self.allowed:raise ValueError()
        path=self.repo/relative
        if not path.resolve().is_relative_to(self.repo):raise ValueError()
        for parent in (path,*path.parents):
            if parent==self.repo:break
            if parent.is_symlink():raise ValueError()
        return path

    def assert_allowed(self):
        changed=set(filter(None,_git(self.repo,'diff','--name-only','-z',self.base,'--').split('\0')))
        changed.update(filter(None,_git(self.repo,'ls-files','--others','-z').split('\0')))
        if not changed<=self.allowed:raise ValueError()
        for relative in changed:
            if not self.safe_path(relative).is_file():raise ValueError()

class CaptureStoreError(RuntimeError):
    """Publication may be uncertain; inspect exact candidate, never retry blindly."""


def retain_capture_object(*,repo,expected_commit,approval_sha256,symbol,name,payload,
                          execution_day,request_id,forbidden_bytes):
    """Return independent digest only after exact commit and durability readback.

The immutable prefix is keyed by a stable approval object's SHA-256, NOT run ID.
This low-level writer cannot authenticate that approval, secret coverage, or
private repository visibility. Integrators must not treat inputs as credentials.
"""
    try:
        if not re.fullmatch('[0-9a-f]{64}',approval_sha256):raise ValueError()
        if not re.fullmatch('[A-Z][A-Z0-9.-]{0,14}',symbol):raise ValueError()
        if name not in LIMITS or type(payload) is not bytes or not 0<len(payload)<=LIMITS[name]:raise ValueError()
        if not isinstance(forbidden_bytes,tuple) or not forbidden_bytes:raise ValueError()
        for secret in forbidden_bytes:
            if type(secret) is not bytes or len(secret)<8 or secret in payload:raise ValueError()
        relative=f'capture-evidence/{approval_sha256}/{symbol}/{name}'
        # Capture-specific checks use hook/fsmonitor-disabled Git throughout.
        def verify(repo, commit):
            # A separate exact-object verifier: never broaden the existing
            # market-data path whitelist to make evidence appear authoritative.
            transport=_CaptureTransport(repo)
            if publication._one_remote_ref(repo,'origin','refs/heads/main',transport=transport)!=commit:raise ValueError()
            transport.read('fetch','--no-tags','--no-recurse-submodules','--write-fetch-head','origin',commit)
            if _git(repo,'rev-parse','FETCH_HEAD')!=commit:raise ValueError()
            if _git(repo,'rev-parse','HEAD')!=commit:raise ValueError()
            if _git(repo,'rev-list','--parents','-n','1',commit)!=f'{commit} {expected_commit}':raise ValueError()
            if _git(repo,'diff-tree','--no-commit-id','--name-status','-r',commit)!=f'A\t{relative}':raise ValueError()
            if _git(repo,'rev-parse',f'{commit}:{relative}')!=blob:raise ValueError()
            tag=f'{publication.REF_PREFIX}{commit}'
            if publication._one_remote_ref(repo,'origin','refs/heads/main',transport=transport)!=commit:raise ValueError()
            found=publication._one_remote_ref(repo,'origin',tag,transport=transport)
            if found not in (None,commit):raise ValueError()
            if found is None:
                transport.once('-c','core.hooksPath=/dev/null','push','origin',f'{commit}:{tag}')
            if publication._one_remote_ref(repo,'origin',tag,transport=transport)!=commit:raise ValueError()
            if publication._one_remote_ref(repo,'origin','refs/heads/main',transport=transport)!=commit:raise ValueError()
            return {'status':'durability_verified','store_commit_sha':commit}
        writer=_EvidenceWriter(Path(repo),expected_commit,execution_day,request_id)
        writer.allowed={relative}
        path=writer.safe_path(relative)
        if path.exists():raise ValueError()
        path.parent.mkdir(parents=True,exist_ok=True)
        path=writer.safe_path(relative)
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(payload);stream.flush();os.fsync(stream.fileno())
        parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(parent)
        finally:os.close(parent)
        if path.read_bytes()!=payload:raise ValueError()
        # Literal-byte Git plumbing bypasses clean filters, attributes and commit
        # hooks. Candidate validation occurs BEFORE any publication, not after.
        writer.assert_allowed()
        blob=_git(writer.repo,'hash-object','-w','--no-filters','--',str(path))
        expected_blob=hashlib.sha1(b'blob '+str(len(payload)).encode()+b'\0'+payload).hexdigest()
        if blob!=expected_blob:raise ValueError()
        _git(writer.repo,'update-index','--add','--cacheinfo',f'100644,{blob},{relative}')
        if _git(writer.repo,'diff','--cached','--name-status',expected_commit)!=f'A\t{relative}':raise ValueError()
        tree=_git(writer.repo,'write-tree')
        commit=_git(writer.repo,'-c','user.name=stock-market-data-bot',
            '-c','user.email=stock-market-data-bot@users.noreply.github.com',
            'commit-tree',tree,'-p',expected_commit,'-m','data: retain unqualified capture evidence')
        if _git(writer.repo,'rev-list','--parents','-n','1',commit)!=f'{commit} {expected_commit}':raise ValueError()
        if _git(writer.repo,'diff-tree','--no-commit-id','--name-status','-r',commit)!=f'A\t{relative}':raise ValueError()
        if _git(writer.repo,'ls-tree',commit,'--',relative)!=f'100644 blob {blob}\t{relative}':raise ValueError()
        if writer.remote_head()!=expected_commit:raise ValueError()
        _git(writer.repo,'update-ref','HEAD',commit,expected_commit)
        # Never execute pre-push hooks with private response bytes available.
        _git(writer.repo,'-c','core.hooksPath=/dev/null','push','origin',f'{commit}:refs/heads/main')
        verify(writer.repo,commit)
        if _git(writer.repo,'rev-parse',f'{commit}:{relative}')!=blob:raise ValueError()
        # The capture-specific verifier reads back main and reuses the existing
        # exact market-data-read/<commit> retention-reference transport.
        return {'sha256':hashlib.sha256(payload).hexdigest(),'blob_sha':blob,
                'store_commit_sha':commit,'path':relative,
                'durability_ref':f'refs/tags/market-data-read/{commit}',
                'market_qualified':False,'production_activation':False}
    except Exception:
        pass
    raise CaptureStoreError('capture_store_publication_unconfirmed_inspect_before_retry')
