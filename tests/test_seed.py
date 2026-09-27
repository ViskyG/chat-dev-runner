"""Offline seed/bootstrap checks. None authenticate to Google."""
import importlib.util
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('bootstrap',ROOT/'scripts/bootstrap_drive.py')
bootstrap=importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)

def test_plan_no_network_and_ordered_sources():
    plan=bootstrap.plan();seen=set()
    for e in plan['entries']:
        assert e['key'] not in seen
        if 'parent' in e: assert e['parent'] in seen
        if 'source' in e: assert (ROOT/e['source']).is_file()
        seen.add(e['key'])
    assert {'root','start-here','command','receipt','state'} <= seen
    assert plan['intended_state']=='UNARMED'

def test_cli_plan_needs_no_google_auth():
    p=subprocess.run([sys.executable,str(ROOT/'scripts/bootstrap_drive.py'),'--plan'],capture_output=True,text=True,check=True)
    report=json.loads(p.stdout)
    assert report['network_calls'] is False
    assert report['mode']=='PLAN_ONLY'

@pytest.mark.parametrize('path',list((ROOT/'drive-seed/00-control/recipes').glob('*.json')))
def test_all_command_recipes_are_valid_but_inert(path):
    v=Draft202012Validator(json.loads((ROOT/'schemas/command.schema.json').read_text()),format_checker=FormatChecker())
    d=json.loads(path.read_text());v.validate(d)
    assert d['dispatch']=='DRAFT'
    assert d['epoch']=='00000000-0000-0000-0000-000000000000'

def test_plain_document_extract():
    doc={'tabs':[{'tabProperties':{'tabId':'t1'},'documentTab':{'body':{'content':[{'paragraph':{'elements':[{'textRun':{'content':'example\n'}}]}}]}}}]}
    assert bootstrap.extract_plain(doc)==('example\n','t1')

@pytest.mark.parametrize('doc',[
 {'tabs':[{'tabProperties':{'tabId':'x'},'childTabs':[{}]}]},
 {'tabs':[{},{}]},
 {'body':{'content':[{'table':{}}]}}
])
def test_unexpected_doc_layout_refused(doc):
    with pytest.raises(RuntimeError):bootstrap.extract_plain(doc)

def test_state_is_atomic_private_json(tmp_path):
    p=tmp_path/'state.json';bootstrap.atomic_json(p,{'x':1});bootstrap.atomic_json(p,{'x':2})
    assert json.loads(p.read_text())=={'x':2}
    assert stat.S_IMODE(p.stat().st_mode)==0o600
    assert list(tmp_path.iterdir())==[p]

def test_operation_output_contract_matches_run_and_common():
    run=json.loads((ROOT/'schemas/run-manifest.schema.json').read_text())
    op=json.loads((ROOT/'schemas/operation-manifest.schema.json').read_text())
    out=json.loads((ROOT/'schemas/output.schema.json').read_text())
    for key in ['$schema','$id','title']:out.pop(key,None)
    assert run['properties']['outputs']==op['properties']['outputs']
    assert run['properties']['outputs']['items']==out
    Draft202012Validator(op,format_checker=FormatChecker()).validate(json.loads((ROOT/'examples/operation-manifest.example.json').read_text()))

def test_application_log_fixture_conforms():
    s=json.loads((ROOT/'schemas/application-log.schema.json').read_text())
    v=Draft202012Validator(s,format_checker=FormatChecker())
    for line in (ROOT/'drive-seed/03-contracts/application-log.example.jsonl').read_text().splitlines():v.validate(json.loads(line))

@pytest.mark.parametrize('path',list((ROOT/'schemas').glob('*.json')))
def test_schemas_are_valid(path):
    Draft202012Validator.check_schema(json.loads(path.read_text()))

@pytest.fixture
def static_server():
    import threading
    from functools import partial
    from http.server import ThreadingHTTPServer
    s=importlib.util.spec_from_file_location('static_server',ROOT/'scripts/serve_prototype.py')
    m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(m.Handler,directory=str(ROOT/'prototype')))
    t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
    yield server
    server.shutdown();server.server_close();t.join(timeout=2)

@pytest.mark.parametrize('method,path,host,expected',[
    ('GET','/','local',200),('GET','/','attacker.example',403),
    ('HEAD','/','attacker.example',403),('POST','/','local',501),
    ('GET','/../config/runner.example.json','local',404)])
def test_static_server_is_local_read_only(static_server,method,path,host,expected):
    import http.client
    port=static_server.server_port
    c=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
    c.request(method,path,headers={'Host':f'127.0.0.1:{port}' if host=='local' else host})
    r=c.getresponse();r.read()
    assert r.status==expected
    if r.status==200:
        assert "connect-src 'none'" in r.getheader('Content-Security-Policy')
        assert r.getheader('X-Content-Type-Options')=='nosniff'
    c.close()
