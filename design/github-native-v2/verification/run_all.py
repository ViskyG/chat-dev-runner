"""Run package-only checks. No installation, provider calls or user project mutation."""
from pathlib import Path
from datetime import datetime,timezone
import contextlib,io,json,os,platform,subprocess,sys,time,unittest
ROOT=Path(__file__).resolve().parents[1]
def main():
    start=time.monotonic();buf=io.StringIO()
    suite=unittest.defaultTestLoader.discover(str(ROOT/'verification'),pattern='test_protocol_model.py')
    result=unittest.TextTestRunner(stream=buf,verbosity=2).run(suite)
    (ROOT/'verification/model-test-output.txt').write_text(buf.getvalue())
    checks=[]
    for script in ('check_contracts.py','git_publication_experiment.py'):
        p=subprocess.run([sys.executable,str(ROOT/'verification'/script)],cwd=ROOT,capture_output=True,text=True,timeout=45)
        checks.append({'script':script,'exit_code':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
    status='PASS' if result.wasSuccessful() and all(c['exit_code']==0 for c in checks) else 'FAIL'
    data={'status':status,'executed_utc':datetime.now(timezone.utc).isoformat(),'duration_seconds':round(time.monotonic()-start,3),'environment':{'python':platform.python_version(),'os':platform.system(),'arch':platform.machine()},'model_tests':result.testsRun,'model_failures':len(result.failures),'model_errors':len(result.errors),'sequential_event_permutations_inside_one_test':120,'other_checks':checks,'scope':'Offline state model, proposed schemas, and disposable local Git only','not_tested':['actual Runner source/regression','live GitHub/Google API','multiprocess provider races','native Windows/macOS installation','five-language projects','Compose','Unity'],'caveats':['FakeMailbox substitutes for provider conditional writes.','Model payload/IDs abstract the full transport schema, which is validated separately.','Model cancellation assumes the stop transition; native cleanup is not implemented by this model.','Sequence permutations are sequential logical orderings, not actual thread concurrency.']}
    (ROOT/'verification/verification-results.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':status,'model_tests':result.testsRun,'errors':len(result.errors),'failures':len(result.failures),'other_exit_codes':[c['exit_code'] for c in checks]}))
    return 0 if status=='PASS' else 1
if __name__=='__main__':raise SystemExit(main())
