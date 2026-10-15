"""Validate proposed schemas/examples and selected semantic invariants; no network."""
from __future__ import annotations
import copy
import json
import sys
from pathlib import Path
import jsonschema

ROOT=Path(__file__).resolve().parents[1]

def semantic_project(value:dict) -> None:
    for task in value['tasks'].values():
        ids=[o['id'] for o in task['outputs']]
        if len(ids)!=len(set(ids)):raise ValueError('duplicate output id')
        if not set(task['quality_gate']['required_outputs'])<=set(ids):raise ValueError('missing required output definition')
        for o in task['outputs']:
            path=o['path']
            if path.startswith(('/', '\\')) or '\\' in path or ':' in path or any(p in ('','.','..') for p in path.split('/')):
                raise ValueError('unsafe output path')
        for step in task['steps']:
            cwd=step['cwd']
            if cwd!='.' and (cwd.startswith(('/', '\\')) or '\\' in cwd or ':' in cwd or any(p in ('','.','..') for p in cwd.split('/'))):
                raise ValueError('unsafe cwd')
            for arg in step['argv']:
                if arg.startswith('{output:') and arg.endswith('}'):
                    name=arg[8:-1]
                    if name not in ids:raise ValueError('unresolved output token')
                    match=next(o for o in task['outputs'] if o['id']==name)
                    if match['source_root']!='out' or any(c in match['path'] for c in '*?['):raise ValueError('non-scalar output token')

def main()->int:
    schemas={}
    for p in sorted((ROOT/'contracts').glob('*.schema.json')):
        v=json.loads(p.read_text());jsonschema.Draft202012Validator.check_schema(v)
        schemas[p.name.split('.')[0]]=jsonschema.Draft202012Validator(v,format_checker=jsonschema.FormatChecker())
    results=[]
    examples={}
    for p in sorted((ROOT/'contracts/examples').glob('*.json')):
        v=json.loads(p.read_text());typ='mailbox' if p.name.startswith('command-') else p.name.split('-')[0]
        schemas[typ].validate(v)
        if typ=='mailbox' and v['dispatch']!='DRAFT':raise ValueError('live command in package')
        if typ=='project':semantic_project(v)
        results.append({'example':p.relative_to(ROOT).as_posix(),'status':'PASS'});examples[p.name]=v
    negatives=[]
    def reject_schema(label,typ,v):
        if schemas[typ].is_valid(v):raise AssertionError('invalid example accepted: '+label)
        negatives.append(label)
    v=copy.deepcopy(examples['command-native-linux.json']);v['slot_token']='not-uuid';reject_schema('bad-slot-uuid','mailbox',v)
    v=copy.deepcopy(examples['command-native-linux.json']);del v['request']['source'];reject_schema('run-missing-source','mailbox',v)
    v=copy.deepcopy(examples['command-shell.json']);del v['request']['shell'];reject_schema('shell-missing-script','mailbox',v)
    v=copy.deepcopy(examples['command-cancel.json']);v['request']['shell']={'dialect':'bash','script':'x'};reject_schema('control-cannot-hide-shell','mailbox',v)
    v=copy.deepcopy(examples['command-native-linux.json']);v['extra_authority']=True;reject_schema('unknown-envelope-field','mailbox',v)
    v=copy.deepcopy(examples['command-container-linux.json']);del v['request']['execution']['backend_id'];reject_schema('container-needs-backend','mailbox',v)
    v=copy.deepcopy(examples['result-local-only.json']);v['artifacts'][0]['sha256']='bad';reject_schema('invalid-result-hash','result',v)
    v=copy.deepcopy(examples['approval-pending.json']);v['items'][0]['owner_decision']='ASSUMED';reject_schema('cannot-assume-approval','approval',v)
    for bad in ['../secret','/etc/passwd','C:/secret','a\\b','a//b']:
        v=copy.deepcopy(examples['project-python.json']);v['tasks']['test']['outputs'][0]['path']=bad
        try:semantic_project(v)
        except ValueError:negatives.append('unsafe-path:'+bad)
        else:raise AssertionError('unsafe path accepted')
    v=copy.deepcopy(examples['environment-native.json']);v['execution']['isolation_claim']='secure_native_sandbox';reject_schema('no-fictional-native-sandbox','environment',v)
    out={'scope':'Design JSON schemas and inert examples only; not runtime integration','schema_count':len(schemas),'valid_examples':results,'negative_checks':negatives,'status':'PASS'}
    (ROOT/'verification/contract-results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
    print(f"PASS: {len(schemas)} schemas, {len(results)} inert examples, {len(negatives)} negative checks")
    return 0
if __name__=='__main__':sys.exit(main())
