import hashlib, json, os, platform, shutil, sys, time
from dataclasses import asdict
from pathlib import Path
from threading import Event
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(ROOT))
from runner_app.domain.project_v2 import parse_project
from runner_app.executors.task_plan_v2 import plan_task
from runner_app.executors.native_v2 import execute_native
from runner_app.outputs.quality_v2 import parse_test_report,evaluate_quality
BASE=Path('/tmp/gp06-toolchains');BASE.chmod(0o700);os.umask(0o077)
EVIDENCE=ROOT/'verification/implementation/GP-06/toolchains'
NODE=Path('/home/visky/.nvm/versions/node/v22.22.2/bin/node')
NPM=Path('/home/visky/.nvm/versions/node/v22.22.2/lib/node_modules/npm/bin/npm-cli.js')
VERSIONS={'python':'3.10.12','node':'v22.22.2','npm':'10.9.7','dotnet':'8.0.131'}
records=[]
for language in ('python','javascript','typescript','csharp'):
    source=ROOT/f'examples/v2/{language}-calculator'
    source_files={str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()}
    source_digest=hashlib.sha256(json.dumps(source_files,sort_keys=True).encode()).hexdigest()
    for cache in ('cold','warm'):
        name=f'{language}-{cache}';slot=BASE/'runs'/name;slot.mkdir(parents=True,mode=0o700)
        workspace=slot/'workspace';shutil.copytree(source,workspace)
        out=slot/'out';out.mkdir(mode=0o700)
        if cache=='warm':
            prior=BASE/'runs'/f'{language}-cold'/'out/.cache'
            if prior.exists():shutil.copytree(prior,out/'.cache')
        tool_paths={'python':Path('/usr/bin/python3')} if language=='python' else {'dotnet':Path('/usr/bin/dotnet')} if language=='csharp' else {'node':NODE,'npm':(NODE,NPM)}
        frozen=parse_project((workspace/'.runner/project.json').read_bytes())
        plan=plan_task(frozen,'test',workspace=workspace,out=out,tool_paths=tool_paths,timeout_budget_seconds=2400,run_id=str(uuid4()),source_sha=source_digest,tool_versions={k:v for k,v in VERSIONS.items() if k in tool_paths})
        log_root=EVIDENCE/name;log_root.mkdir(mode=0o700)
        env=dict(plan.environment)
        env.update(PATH=str(NODE.parent)+':/usr/bin:/bin',PIP_CONFIG_FILE='/dev/null',PIP_INDEX_URL='https://pypi.org/simple',PIP_FIND_LINKS=str(ROOT/'runner_app/git/python_quick_wheels'),PIP_CACHE_DIR=str(out/'.cache/pip'),PIP_DISABLE_PIP_VERSION_CHECK='1',PIP_RETRIES='0',PIP_DEFAULT_TIMEOUT='20',NPM_CONFIG_USERCONFIG=str(BASE/'npm-user.conf'),NPM_CONFIG_GLOBALCONFIG=str(BASE/'npm-global.conf'),NPM_CONFIG_FETCH_RETRIES='0',NPM_CONFIG_FETCH_TIMEOUT='20000',NPM_CONFIG_AUDIT='false',NPM_CONFIG_FUND='false',NPM_CONFIG_UPDATE_NOTIFIER='false',NUGET_HTTP_CACHE_PATH=str(out/'.cache/nuget-http'),NUGET_ENHANCED_MAX_NETWORK_TRY_COUNT='1',NUGET_ENHANCED_NETWORK_RETRY_DELAY_MILLISECONDS='500',DOTNET_NOLOGO='1',DOTNET_GENERATE_ASPNET_CERTIFICATE='false',DOTNET_CLI_USE_MSBUILD_SERVER='0',MSBUILDDISABLENODEREUSE='1')
        cache_before=sum(1 for p in (out/'.cache').rglob('*') if p.is_file()) if (out/'.cache').exists() else 0
        steps=[];test_outcomes=[];started=time.monotonic()
        for step in plan.steps:
            result=execute_native(list(step.argv),cwd=step.cwd,env=env,output_dir=log_root/step.id,artifact_dir=out,timeout_seconds=step.timeout_seconds,cancel=Event(),max_log_bytes=2*1024*1024)
            item={'id':step.id,'phase':step.phase,'argv':list(step.argv),'cwd':str(step.cwd),'outcome':result.outcome,'exit_code':result.exit_code,'cleanup_verified':result.cleanup_verified,'duration_seconds':round(result.duration_seconds,3),'error_code':result.error_code,'stdout_sha256':hashlib.sha256(result.stdout_path.read_bytes()).hexdigest() if result.stdout_path.exists() else None,'stderr_sha256':hashlib.sha256(result.stderr_path.read_bytes()).hexdigest() if result.stderr_path.exists() else None}
            steps.append(item)
            if step.phase=='test':test_outcomes.append(result.outcome)
            print(json.dumps({'run':name,**item}),flush=True)
            if not result.cleanup_verified:
                raise RuntimeError('CLEANUP_UNVERIFIED; stopped subsequent qualification runs')
            if result.outcome!='PASSED':break
        reports={};available=[];report_files=[]
        for output in plan.outputs:
            if output.path.is_file():
                raw=output.path.read_bytes();frozen_report=log_root/output.path.name;frozen_report.write_bytes(raw)
                parsed=parse_test_report(raw,output.format);reports[output.id]=parsed;available.append(output.id)
                report_files.append({'id':output.id,'path':str(frozen_report.relative_to(EVIDENCE)),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'parse_status':parsed.status,'summary':parsed.summary,'findings':list(parsed.findings)})
        quality=evaluate_quality(plan.quality_gate,reports,available_outputs=available,execution_outcomes=test_outcomes)
        installed={}
        if language=='python' and (out/'.environment/python/bin/python').exists():
            import subprocess
            result=subprocess.run([str(out/'.environment/python/bin/python'),'-m','pytest','--version'],cwd=workspace,env={'PATH':'/usr/bin:/bin'},stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=10,check=False)
            installed['pytest']={'exit_code':result.returncode,'version':result.stdout.decode().strip()}
        for package in ('vitest','typescript'):
            path=workspace/f'node_modules/{package}/package.json'
            if path.is_file():installed[package]={'version':json.loads(path.read_bytes())['version'],'version_source':'installed-package-metadata'}
        record={'name':name,'language':language,'cache_state':cache,'cache_files_before':cache_before,'cache_files_after':sum(1 for p in (out/'.cache').rglob('*') if p.is_file()) if (out/'.cache').exists() else 0,'source_sha256':source_digest,'source_files':source_files,'project_digest':plan.project_digest,'task_digest':plan.task_digest,'lock_hashes':dict(plan.lock_hashes),'tool_bindings':[asdict(tool) for tool in plan.tools],'installed_tools':installed,'steps':steps,'reports':report_files,'quality':{'quality_gate':quality.quality_gate,'test_outcome':quality.test_outcome,'counts':dict(quality.counts),'findings':list(quality.findings)},'duration_seconds':round(time.monotonic()-started,3),'qualification_scope':'tools/profile/report boundary; not full Runner route','host_os':platform.system(),'host_arch':platform.machine()}
        (log_root/'record.json').write_text(json.dumps(record,indent=2)+'\n');records.append(record)
        (EVIDENCE/'runs.json').write_text(json.dumps(records,indent=2)+'\n')
print(json.dumps({'complete':True,'runs':len(records),'passed':sum(r['quality']['quality_gate']=='PASSED' for r in records)}),flush=True)
