"""Read-only facade over reviewed exact Git fetch/ref primitives.

No new credential or write permission is required. The transport's operation
allowlist rejects push before delegation; all inherited object/publication entry
points are blocked. Only exact local ref materialization may change the clone.
"""
import re
from scripts.daily_basis_remote import RemoteGitStore,GitHubGitTransport,_need,_safe


class _ReadTransport:
    def __init__(self,inner):self._inner=inner
    def metadata(self):return self._inner.metadata()
    def git(self,*args):
        _need(args and args[0] in ('ls-remote','fetch'))
        return self._inner.git(*args)


class InitialReadGitStore(RemoteGitStore):
    def __init__(self,repo,token,*,source_recheck,platform_guard,
            expected_repository_id=1335101677,expected_owner_id=None,transport=None):
        readonly=_ReadTransport(transport if transport is not None else GitHubGitTransport(repo,token))
        super().__init__(repo,token,source_recheck=source_recheck,platform_guard=platform_guard,
            expected_repository_id=expected_repository_id,expected_owner_id=expected_owner_id,transport=readonly)

    def _check(self):
        self._validate_local();self._source_recheck();self._platform_guard()
        value=self._transport.metadata()
        _need(type(value) is dict and value.get('private') is True and type(value.get('id')) is int
            and value['id']==self._repository_id and value.get('full_name')=='yuatom/stock-market-data-store'
            and value.get('default_branch')=='main')
        owner=value.get('owner');permissions=value.get('permissions')
        _need(type(owner) is dict and owner.get('login')=='yuatom' and type(owner.get('id')) is int
            and owner['id']>0 and (self._owner_id is None or owner['id']==self._owner_id))
        _need(type(permissions) is dict and permissions.get('pull') is True)
        if self._owner_id is None:self._owner_id=owner['id']

    def _ref_scope(self,ref,*,write=False):
        _need(not write and type(ref) is str and (ref=='refs/heads/main'
            or re.fullmatch(r'refs/heads/basis-read-receipts/[0-9a-f]{40}/[0-9a-f]{64}',ref)
            or re.fullmatch(r'refs/tags/market-data-read/[0-9a-f]{40}',ref)))
        return super()._ref_scope(ref,write=False)

    @_safe
    def commit(self,*args,**kwargs):_need(False)
    @_safe
    def cas(self,*args,**kwargs):_need(False)
    @_safe
    def publish(self,*args,**kwargs):_need(False)
