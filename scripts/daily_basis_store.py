"""Real local Git object/CAS adapter. No network operations or production claim.

A single-host flock is sufficient only for local validation. A hosted adapter
must additionally hold the existing market-data-collector Actions concurrency
owner and independently verify remote CAS/durability. It must not reuse this
local adapter as evidence that a remote publication happened.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import subprocess
import tempfile
from scripts import provider_basis_snapshot as B


class LocalGitStore:
    def __init__(self, repo):
        path=Path(repo).absolute()
        B._need(not any(p.is_symlink() for p in (path,*path.parents)))
        self.g=B._store(path);self.repo=self.g.repo;self._lock=None

    @contextmanager
    def lock(self):
        B._need(self._lock is None)
        fd=os.open(self.g.gitdir/'daily-basis-writer.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            B._need(os.fstat(fd).st_nlink==1)
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);self._lock=fd
            yield self
        finally:
            self._lock=None;os.close(fd)

    def assert_locked(self):
        B._need(self._lock is not None)
        stat=os.fstat(self._lock);path=os.stat(self.g.gitdir/'daily-basis-writer.lock',follow_symlinks=False)
        B._need((stat.st_dev,stat.st_ino)==(path.st_dev,path.st_ino))
        self.g.assert_unmodified_graph()

    def ref(self, ref):
        B._need(re.fullmatch(r'refs/(?:heads|tags)/[A-Za-z0-9_/-]+',ref))
        raw=self.g.read('for-each-ref','--format=%(refname) %(objectname) %(objecttype) %(symref)',ref)
        if not raw:return None
        match=re.fullmatch(re.escape(ref.encode())+rb' ([0-9a-f]{40}) commit \n',raw)
        B._need(match);return match.group(1).decode()

    def materialize_ref(self, ref):
        """Resolve an existing exact local ref; hosted stores fetch and verify it."""
        self.assert_locked()
        identity=self.ref(ref);B._hex(identity)
        return identity

    def head(self):
        result=self.ref('refs/heads/main');B._hex(result);return result

    def exists(self,commit,path):
        B._hex(commit);self.path(path)
        return bool(self.g.read('ls-tree','-z',commit,'--',path))

    @staticmethod
    def path(path):
        B._need(type(path) is str and re.fullmatch('[A-Za-z0-9_./-]+',path) and
            all(p not in ('','.','..') for p in path.split('/')))

    def read(self,commit,path):
        return B._blob(self.g,commit,path)

    def _write(self,*args,raw=None,env=None):
        result=subprocess.run(['git','-C',str(self.repo),'--no-replace-objects',
            '-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false',
            '-c','protocol.allow=never','-c','core.fsync=committed',
            '-c','user.name=Local basis validation','-c','user.email=basis@example.invalid',*args],
            env=env or self.g.env,input=raw,capture_output=True,timeout=30)
        B._need(result.returncode==0);return result.stdout.decode('ascii').strip()

    def commit(self,parent,updates,*,replace):
        self.assert_locked();B._hex(parent);self.g.commit(parent)
        B._need(type(updates) is dict and updates and type(replace) is set and replace<=set(updates))
        for path,raw in updates.items():
            self.path(path);B._need(type(raw) is bytes and 0<len(raw)<=B.MAX_BYTES)
            B._need(not self.exists(parent,path) or path in replace)
        with tempfile.TemporaryDirectory(prefix='basis-index-',dir=self.g.gitdir) as directory:
            env=dict(self.g.env,GIT_INDEX_FILE=str(Path(directory)/'index'))
            self._write('read-tree',parent,env=env)
            for path,raw in updates.items():
                blob=self._write('hash-object','-w','--stdin',raw=raw)
                B._need(blob==B.blob_sha(raw))
                self._write('update-index','--add','--cacheinfo',f'100644,{blob},{path}',env=env)
            tree=self._write('write-tree',env=env)
            candidate=self._write('commit-tree',tree,'-p',parent,'-m','local: finite immutable daily basis transaction')
        B._need(self.g.commit(candidate)[1]==(parent,))
        names=self.g.text('diff-tree','--no-commit-id','--name-only','--no-renames','-r',parent,candidate).splitlines()
        B._need(set(names)==set(updates))
        for path,raw in updates.items():B._need(self.read(candidate,path)==raw)
        return candidate

    def cas(self,ref,new,expected):
        self.assert_locked();B._hex(new);B._hex(expected)
        B._need(self.ref(ref)==(None if expected=='0'*40 else expected))
        tag='refs/tags/market-data-read/'+new
        existing=self.ref(tag);B._need(existing in (None,new))
        tag_line=('create '+tag+' '+new) if existing is None else ('verify '+tag+' '+new)
        commands='start\nupdate '+ref+' '+new+' '+expected+'\n'+tag_line+'\nprepare\ncommit\n'
        self._write('update-ref','--stdin',raw=commands.encode('ascii'))
        B._need(self.ref(tag)==new)
        # A failed readback is uncertain, never a reason to retry or rollback.
        B._need(self.ref(ref)==new)
        self.g.commit(new)

    def publish(self,expected,updates,*,replace):
        B._need(self.head()==expected)
        candidate=self.commit(expected,updates,replace=replace)
        self.cas('refs/heads/main',candidate,expected)
        return candidate
