"""One-shot hidden Gateway launcher with durable output and exit evidence. No retries."""


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))

import os,sys,json,pathlib,subprocess,datetime,hashlib,time
ROOT=pathlib.Path(_public_project_path('logs', 'gateway_observed'))
GATEWAY=pathlib.Path('<USER_HOME>/.openclaw/gateway.cmd'.replace('<USER_HOME>', str(__import__('pathlib').Path.home())))
def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def save(p,v):
 t=p.with_suffix('.tmp');t.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(t,p)
def run(argv,tag):
 stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ');folder=ROOT/(tag+'-'+stamp);folder.mkdir(parents=True,exist_ok=False)
 receipt={'started_at_utc':now(),'supervisor_pid':os.getpid(),'supervisor_parent_pid':os.getppid(),'mode':tag,'automatic_restarts':0,'gateway_cmd_sha256':hashlib.sha256(GATEWAY.read_bytes()).hexdigest()if tag=='gateway'else None}
 with (folder/'stdout.log').open('ab',buffering=0)as stdout,(folder/'stderr.log').open('ab',buffering=0)as stderr:
  child=subprocess.Popen(argv,cwd=str(GATEWAY.parent),stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,creationflags=subprocess.CREATE_NO_WINDOW)
  receipt['child_pid']=child.pid;save(folder/'lifecycle.json',receipt);save(ROOT/'latest.json',{'folder':str(folder),**receipt})
  while child.poll()is None:
   save(folder/'heartbeat.json',{'at_utc':now(),'supervisor_pid':os.getpid(),'child_pid':child.pid,'child_running':True});time.sleep(5)
  rc=child.returncode;receipt.update(ended_at_utc=now(),exit_code=rc,exit_code_hex=f'0x{rc&0xffffffff:08x}');save(folder/'lifecycle.json',receipt);save(ROOT/'latest.json',{'folder':str(folder),**receipt})
 return rc
if __name__=='__main__':
 if sys.argv[1:]==['--self-test']:
  rc=run([sys.executable,'-I','-c',"import sys;print('stdout-capture-ok');print('stderr-capture-ok',file=sys.stderr);sys.exit(7)"],'selftest');assert rc==7
 elif sys.argv[1:]==['--run']:
  raise SystemExit(run([os.environ.get('COMSPEC','C:/Windows/System32/cmd.exe'),'/d','/c',str(GATEWAY)],'gateway'))
 else:raise SystemExit('Use --run or --self-test')
