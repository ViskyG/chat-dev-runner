"""Real local Git experiment. Uses disposable repositories, no GitHub/network/user checkout.

Proves a result-only commit can be rebuilt on a concurrently advanced branch, with a
rejected non-fast-forward attempt and preserved developer source. NOT provider acceptance.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]

def main()->None:
    with tempfile.TemporaryDirectory(prefix='cdr-design-git-') as tmp:
        base=Path(tmp);remote=base/'remote.git';author=base/'author';publisher=base/'publisher';home=base/'home';home.mkdir()
        env=dict(os.environ);env.update({'HOME':str(home),'GIT_CONFIG_NOSYSTEM':'1','GIT_TERMINAL_PROMPT':'0'})
        for k in ['GIT_DIR','GIT_WORK_TREE','GIT_INDEX_FILE','GIT_SSH_COMMAND','GIT_CONFIG_COUNT']:
            env.pop(k,None)
        def git(cwd:Path,*args:str,check:bool=True)->subprocess.CompletedProcess[str]:
            return subprocess.run(['git','-c','user.name=Design Fixture','-c','user.email=fixture@example.invalid','-c','core.hooksPath='+str(base/'no-hooks'),*args],cwd=cwd,env=env,text=True,capture_output=True,timeout=15,check=check)
        git(base,'init','--bare',str(remote));git(remote,'symbolic-ref','HEAD','refs/heads/main')
        git(base,'clone',str(remote),str(author));(author/'app.txt').write_text('source-A\n');git(author,'add','app.txt');git(author,'commit','-m','fixture A');git(author,'push','origin','HEAD:refs/heads/main')
        tested=git(author,'rev-parse','HEAD').stdout.strip()
        git(base,'clone',str(remote),str(publisher))
        (author/'app.txt').write_text('source-B\n');git(author,'add','app.txt');git(author,'commit','-m','developer B');git(author,'push','origin','main')
        git(publisher,'fetch','origin','main');git(publisher,'reset','--hard','origin/main')
        parent_b=git(publisher,'rev-parse','HEAD').stdout.strip()
        result_rel=Path('.runner/results/linux-runner/request-fixture/result.json')
        payload={'tested_commit':tested,'execution':'FAILED','note':'Local experiment only; result is intentionally for A'}
        def write_result():
            out=publisher/result_rel;out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(payload,sort_keys=True)+'\n');git(publisher,'add',result_rel.as_posix());git(publisher,'commit','-m','add frozen result for A')
        write_result();stale_result=git(publisher,'rev-parse','HEAD').stdout.strip()
        # Developer advances the source after publisher has constructed a result commit.
        (author/'app.txt').write_text('source-D\n');git(author,'add','app.txt');git(author,'commit','-m','developer D');git(author,'push','origin','main');developer_d=git(author,'rev-parse','HEAD').stdout.strip()
        rejected=git(publisher,'push','origin','HEAD:refs/heads/main',check=False)
        assert rejected.returncode!=0, 'stale concurrent publication should be rejected'
        git(publisher,'fetch','origin','main');git(publisher,'reset','--hard','origin/main');write_result();git(publisher,'push','origin','HEAD:refs/heads/main')
        published=git(publisher,'rev-parse','HEAD').stdout.strip()
        assert git(remote,'show','refs/heads/main:app.txt').stdout=='source-D\n'
        actual=json.loads(git(remote,'show','refs/heads/main:'+result_rel.as_posix()).stdout)
        assert actual['tested_commit']==tested
        changed=git(remote,'diff','--name-only',developer_d,published).stdout.splitlines()
        assert changed==[result_rel.as_posix()], changed
        assert git(remote,'rev-parse',published+'^').stdout.strip()==developer_d
        # A second runner's independently namespaced report is additive in the same branch.
        second=publisher/'.runner/results/windows-runner/other-request/summary.md';second.parent.mkdir(parents=True,exist_ok=True);second.write_text('Second runner report fixture\n');git(publisher,'add',second.relative_to(publisher).as_posix());git(publisher,'commit','-m','second runner result');git(publisher,'push','origin','HEAD:refs/heads/main')
        assert json.loads(git(remote,'show','refs/heads/main:'+result_rel.as_posix()).stdout)==payload
        assert git(remote,'show','refs/heads/main:app.txt').stdout=='source-D\n'
        result={'status':'PASS','scope':'Disposable local bare Git; no GitHub API or network','git_version':git(base,'--version').stdout.strip(),'tested_commit':tested,'parent_before_race':parent_b,'stale_result_commit':stale_result,'concurrent_developer_commit':developer_d,'published_result_commit':published,'non_fast_forward_rejected':True,'source_preserved':True,'result_only_diff':changed,'two_runner_paths_preserved':True,'temporary_repositories_removed_on_exit':True}
    (ROOT/'verification/git-experiment-results.json').write_text(json.dumps(result,indent=2)+'\n')
    print('PASS: stale publication rejected; same-branch result-only retry preserves current source and both runner results')
if __name__=='__main__':main()
