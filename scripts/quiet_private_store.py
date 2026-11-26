"""Quiet private Git for the reviewed collector workflow, existing token only.

No credential is written into Git configuration, command arguments or logs.
Private ref/commit names emitted by Git are captured into private temporary file
handles and never forwarded to public Actions output. All failures are value-free.
The run mode executes only the immutable workflow's stdin shell program; it is
not a user-content execution interface. No automatic retry is added here.
"""
import argparse
import base64
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.run_private_authority_route import SAFETY,STORE_URL


def git_environment(token):
    if not isinstance(token,str) or not re.fullmatch('[A-Za-z0-9_.-]{8,4096}',token):raise ValueError()
    env={k:v for k,v in os.environ.items() if not k.startswith('GIT_') and k not in ('MARKET_DATA_STORE_TOKEN','TWELVE_DATA_API_KEY')}
    env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_SYSTEM='/dev/null',GIT_CONFIG_GLOBAL='/dev/null',GIT_TERMINAL_PROMPT='0',GIT_NO_REPLACE_OBJECTS='1',GIT_NO_LAZY_FETCH='1',LC_ALL='C')
    settings=[(SAFETY[i+1].split('=',1)[0],SAFETY[i+1].split('=',1)[1]) for i in range(0,len(SAFETY),2)]
    settings.append(('http.'+STORE_URL+'.extraheader','AUTHORIZATION: basic '+base64.b64encode(('x-access-token:'+token).encode()).decode()))
    env['GIT_CONFIG_COUNT']=str(len(settings))
    for i,(key,value) in enumerate(settings):env['GIT_CONFIG_KEY_'+str(i)]=key;env['GIT_CONFIG_VALUE_'+str(i)]=value
    return env


def quiet_call(args,*,env,stdin=None,cwd=None,timeout=120):
    with tempfile.TemporaryFile(mode='w+b') as sink:
        result=subprocess.run(args,input=stdin,cwd=cwd,env=env,stdout=sink,stderr=sink,timeout=timeout)
        if result.returncode!=0:raise ValueError()
        sink.seek(0,2)
        if sink.tell()>1024*1024:raise ValueError()


def clone_private_store(destination,token):
    from scripts.run_daily_basis_route import GitHubAPI
    data=GitHubAPI(token).request('GET','/repos/yuatom/stock-market-data-store')
    if not (data.get('id')==1335101677 and data.get('private') is True and data.get('full_name')=='yuatom/stock-market-data-store' and data.get('default_branch')=='main' and data['owner']['login']=='yuatom'):raise ValueError()
    path=Path(destination).absolute()
    if path.exists() or any(p.is_symlink() for p in (path,*path.parents)):raise ValueError()
    env=git_environment(token)
    quiet_call(['git',*SAFETY,'clone','--no-checkout','--no-local','--origin','origin',STORE_URL,str(path)],env=env)
    quiet_call(['git','-C',str(path),*SAFETY,'checkout','main'],env=env)
    # Remote URLs/refspecs only: the token existed only in child environment.
    raw=subprocess.run(['git','-C',str(path),'config','--local','--list'],capture_output=True,env=env,timeout=30)
    if raw.returncode or b'extraheader' in raw.stdout.lower() or token.encode() in raw.stdout:raise ValueError()


def verify_capture_availability(path, manifest, env):
    """Only fixed native Verify may pass one opaque line; no general stdout."""
    expected_host={
        'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':'yuatom/stock-market-data',
        'GITHUB_EVENT_NAME':'push','GITHUB_REF':'refs/heads/collector-requests','GITHUB_JOB':'collect',
    }
    if (any(env.get(k)!=v for k,v in expected_host.items())
            or not re.fullmatch('[0-9a-f]{40}',env.get('GITHUB_SHA',''))
            or any(not re.fullmatch('[1-9][0-9]{0,19}',env.get(k,''))
                   for k in ('GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT'))
            or not re.fullmatch(r'collector-state/[0-9]{4}-[0-9]{2}/'
                                r'[0-9]{4}-[0-9]{2}-[0-9]{2}-premarket-[0-9a-f]{16}-publication[.]json',
                                manifest)):
        raise ValueError()
    head=subprocess.run(['git','rev-parse','HEAD'],cwd=path,capture_output=True,env=env,timeout=30)
    if head.returncode or not re.fullmatch(rb'[0-9a-f]{40}\n',head.stdout):
        raise ValueError()
    command=[sys.executable,'-B',str(ROOT/'scripts/verify_store_publication.py'),
             '--store-root','.','--expected-commit',head.stdout.strip().decode('ascii'),
             '--writer-status','store_persisted','--required-manifest','data/market-data/'+manifest,
             '--emit-capture-availability-commitment']
    with tempfile.TemporaryFile(mode='w+b') as stdout, tempfile.TemporaryFile(mode='w+b') as stderr:
        result=subprocess.run(command,cwd=path,env=env,stdin=subprocess.DEVNULL,
                              stdout=stdout,stderr=stderr,timeout=600)
        if result.returncode:
            raise ValueError()
        if stderr.tell():
            return None
        stdout.seek(0)
        size=len(b'CAPTURE_AVAILABILITY_V2 ')+64+1
        line=stdout.read(size+1)
        if re.fullmatch(rb'CAPTURE_AVAILABILITY_V2 [0-9a-f]{64}\n',line):
            return spool_capture_availability(line, env)
    return False


def spool_capture_availability(line, env):
    """Create one fixed raw artifact only; unknown or stale files never qualify."""
    if not re.fullmatch(rb'CAPTURE_AVAILABILITY_V2 [0-9a-f]{64}\n',line):
        return False
    run, attempt = env.get('GITHUB_RUN_ID',''), env.get('GITHUB_RUN_ATTEMPT','')
    if any(not re.fullmatch('[1-9][0-9]{0,19}',v) for v in (run,attempt)):
        return False
    temp=env.get('RUNNER_TEMP','')
    if not os.path.isabs(temp):
        return False
    filename=f'capture-availability-v2-{run}-{attempt}-collect.txt'
    try:
        directory=os.open(temp,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            fd=os.open(filename,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
            try:
                return os.write(fd,line)==len(line)
            finally:
                os.close(fd)
        finally:
            os.close(directory)
    except OSError:
        return False


class _QuietCaptureBridgeParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError()  # argparse's default output can echo the private argv.


def main(argv=None):
    try:
        os.umask(0o077)
        argv=sys.argv[1:] if argv is None else argv
        cls=_QuietCaptureBridgeParser if argv[:1]==['verify-capture-availability'] else argparse.ArgumentParser
        parser=cls();sub=parser.add_subparsers(dest='mode',required=True)
        clone=sub.add_parser('clone');clone.add_argument('--destination',required=True)
        run=sub.add_parser('run');run.add_argument('--repo',required=True)
        verify=sub.add_parser('verify-capture-availability');verify.add_argument('--repo',required=True)
        verify.add_argument('--manifest',required=True)
        args=parser.parse_args(argv);token=os.environ.get('MARKET_DATA_STORE_TOKEN','')
        if args.mode=='clone':clone_private_store(args.destination,token)
        else:
            path=Path(args.repo).resolve();env=git_environment(token)
            origin=subprocess.run(['git','-C',str(path),'remote','get-url','--all','origin'],capture_output=True,env=env,timeout=30)
            if origin.returncode or origin.stdout.decode().strip()!=STORE_URL:raise ValueError()
            if args.mode=='verify-capture-availability':
                if verify_capture_availability(path,args.manifest,env):
                    print('CAPTURE_AVAILABILITY_ARTIFACT_READY');return 0
            else:
                script=sys.stdin.buffer.read(65537)
                if not 0<len(script)<=65536:raise ValueError()
                quiet_call(['bash','-euo','pipefail'],env=env,stdin=script,cwd=path,timeout=600)
        print('PRIVATE_STORE_OPERATION_COMPLETE');return 0
    except Exception:print('PRIVATE_STORE_OPERATION_STOPPED');return 2


if __name__=='__main__':raise SystemExit(main())
