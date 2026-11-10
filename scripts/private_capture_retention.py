"""Create-only local private capture files. No remote/market authority is implied.

An existing or partial destination is never resumed, overwritten or cleaned up.
The caller must separately authenticate its exact destination and request scope.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading

from raw_response_evidence import MAX_RAW_BYTES, MAX_ENVELOPE_BYTES, validate_request, verify_envelope


class RetentionError(RuntimeError):
    """Value-free failure; file outcome may be unknown, never retry blindly."""


def _safe(action):
    try:
        return action()
    except Exception:
        pass
    raise RetentionError('private_capture_retention_failed')


def _require(condition):
    if not condition:
        raise ValueError('private capture boundary invalid')


def _encoded(value):
    return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)+'\n').encode()


def _absolute(value):
    path=Path(value)
    # POSIX permits a distinct '//' anchor, while Linux resolves it like '/'.
    # Reject it rather than compare unlike lexical anchors for protected roots.
    _require(path.is_absolute() and path.anchor=='/' and '..' not in path.parts and path.name not in ('','.','..'))
    return path


def _open_directory(path):
    """Traverse every component without following symlinks, including parents."""
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in _absolute(path).parts[1:]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _private(info, directory=False):
    _require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) and
             info.st_uid==os.geteuid() and info.st_mode&0o077==0)
    if not directory:
        _require(info.st_nlink==1)


class PrivateCaptureRetention:
    """One local private destination and one attempted response, with fixed names."""
    def __init__(self,destination,request,*,excluded_roots):
        self._lock=threading.RLock()
        self._fd=None
        self._state='new'
        self._raw_sha=None
        self._request_bytes=b''
        def initialize():
            self.destination=_absolute(destination)
            self.request=validate_request(request)
            self._request_bytes=_encoded(self.request)
            _require(isinstance(excluded_roots,(tuple,list)) and len(excluded_roots)>=2)
            for root in excluded_roots:
                protected=_absolute(root).resolve(strict=True)
                _require(not(self.destination==protected or self.destination.is_relative_to(protected)
                             or protected.is_relative_to(self.destination)))
            parent=_open_directory(self.destination.parent)
            try:
                _private(os.fstat(parent),directory=True)
                os.mkdir(self.destination.name,0o700,dir_fd=parent)
                self._fd=os.open(self.destination.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                _private(os.fstat(self._fd),directory=True)
                self._identity=(os.fstat(self._fd).st_dev,os.fstat(self._fd).st_ino)
                self._write('request.json',self._request_bytes,65536)
                os.fsync(parent)
            finally:
                os.close(parent)
        try:
            _safe(initialize)
        except BaseException:
            try:
                self.close()
            except Exception:
                pass  # Preserve the primary failure; an uncertain close is not retried.
            raise

    def _location(self):
        _require(self._fd is not None)
        fd=_open_directory(self.destination)
        try:
            info=os.fstat(fd);_private(info,directory=True)
            _require((info.st_dev,info.st_ino)==self._identity)
        finally:
            os.close(fd)

    def _read(self,name,bound):
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=self._fd)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno());_private(info)
            _require(0<info.st_size<=bound)
            payload=stream.read(bound+1)
            _require(len(payload)==info.st_size)
            return payload

    def _write(self,name,payload,bound):
        self._location()
        _require(type(payload) is bytes and 0<len(payload)<=bound)
        fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=self._fd)
        with os.fdopen(fd,'wb') as stream:
            _require(stream.write(payload)==len(payload))
            stream.flush();os.fsync(stream.fileno())
        os.fsync(self._fd)
        self._location()
        _require(self._read(name,bound)==payload)

    def _transition(self,expected,next_state,action):
        with self._lock:
            def perform():
                previous=self._state
                self._state='failed'
                _require(previous==expected)
                self._location()
                _require(self._read('request.json',65536)==self._request_bytes)
                result=action()
                self._state=next_state
                return result
            return _safe(perform)

    def claim_attempt(self,*,scope_sha256=None):
        """Persist the no-retry marker before the unchanged external HTTP guard."""
        def claim():
            _require(scope_sha256 is None or (type(scope_sha256) is str and re.fullmatch(r'[0-9a-f]{64}',scope_sha256)))
            self._write('attempt.json',_encoded({'kind':'capture_attempt_claim','request_sha256':
                hashlib.sha256(self._request_bytes).hexdigest(),'scope_sha256':scope_sha256,'policy':'no_retry'}),65536)
        return self._transition('new','attempted',claim)

    def retain_raw(self,payload):
        def retain():
            self._write('response.raw',payload,MAX_RAW_BYTES)
            self._raw_sha=hashlib.sha256(payload).hexdigest()
            return self._raw_sha
        return self._transition('attempted','raw',retain)

    def retain_envelope(self,payload):
        def retain():
            _require(type(payload) is bytes and 0<len(payload)<=MAX_ENVELOPE_BYTES)
            digest=hashlib.sha256(payload).hexdigest()
            value=verify_envelope(payload,digest)
            retained=self._read('response.raw',MAX_RAW_BYTES)
            _require(hashlib.sha256(retained).hexdigest()==self._raw_sha and value['raw_response']==retained)
            _require(_encoded(value['request'])==self._request_bytes)
            self._write('envelope.json',payload,MAX_ENVELOPE_BYTES)
            receipt={'status':'LOCAL_CAPTURE_CONTENTS_ONLY','raw_response_sha256':self._raw_sha,
                'envelope_sha256':digest,'request_sha256':hashlib.sha256(self._request_bytes).hexdigest(),
                'hosted_durability_authenticated':False,'market_fact_authority':False}
            self._write('completion.json',_encoded(receipt),65536)
            return digest
        return self._transition('raw','complete',retain)

    def close(self):
        with self._lock:
            if self._fd is not None:
                fd,self._fd=self._fd,None
                _safe(lambda:os.close(fd))

    def __enter__(self):
        return self

    def __exit__(self,exception_type,exception,traceback):
        if exception_type is None:
            self.close()
        else:
            try:
                self.close()
            except Exception:
                pass  # Do not replace a capture failure with a cleanup failure.
