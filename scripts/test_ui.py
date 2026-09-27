#!/usr/bin/env python3
"""Browser interaction checks for the offline prototype.

Uses Chromium set_content (same bundled HTML/JS/CSS) because managed execution environments
may block local navigation. Does not assert OAuth, filesystem, VS Code or backend integration.
"""
from pathlib import Path
import json,os,sys,time
from playwright.sync_api import sync_playwright
from build_single_file import build
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'verification';OUT.mkdir(exist_ok=True)
results=[]
def record(name,condition=True):
    assert condition,name
    results.append({'check':name,'passed':True})
with sync_playwright() as p:
    exe=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium')
    browser=p.chromium.launch(executable_path=exe,headless=True,args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1600,'height':1120},device_scale_factor=1)
    errors=[];requests=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('request',lambda r:requests.append(r.url))
    html=build().read_text()
    def fresh():
        page.set_content(html,wait_until='load');page.wait_for_timeout(400)
        assert page.evaluate('window.RUNNER_DEMO.mode')=='offline-ui-only'
    def action(name,value=None):
        sel=f'[data-action="{name}"]'+(f'[data-value="{value}"]' if value is not None else '')
        page.locator(sel).first.click()
    def snap():return page.evaluate('window.RUNNER_DEMO.snapshot()')
    def close():
        if page.locator('[role="dialog"]').count():page.keyboard.press('Escape')
    fresh();record('Offline demo starts with explicit simulation badge',page.get_by_text('ДЕМО · БЕЗ ПОДКЛЮЧЕНИЙ').is_visible())
    record('Initial project/workspace scope',snap()['workspace']=='rc-dashboard')
    page.screenshot(path=str(OUT/'ui-dark.png'),full_page=True)
    action('theme');record('Light theme',page.evaluate('document.documentElement.dataset.theme')=='light')
    page.wait_for_timeout(400);page.screenshot(path=str(OUT/'ui-light.png'),full_page=True);action('theme')
    for tab in ['console','logs','tests','artifacts','summary']:
        action('tab',tab);record('Output tab: '+tab,page.locator(f'[data-action="tab"][data-value="{tab}"]').get_attribute('aria-selected')=='true')
    action('tab','console');page.select_option('#console-stream','stderr');record('stderr view',page.locator('#console-text').inner_text()!='')
    action('tab','logs');page.select_option('#log-level','ERROR');record('Structured log level filter','ERROR' in page.locator('#app-log').inner_text())
    action('code-menu');record('VS Code menu lists all project worktrees',page.locator('[data-action="show-code-path"]').count()==3)
    action('show-code-path','airflow-refactor');record('Editor path targets selected worktree','workspaces/airflow-refactor' in page.locator('textarea').input_value());close()
    action('branch');record('Dirty workspace blocks branch switch',page.locator('[data-action="switch-branch"]').is_disabled());close()
    action('workspace','airflow-refactor');action('branch');page.fill('#target-branch','feature/review');action('switch-branch')
    record('Clean workspace retarget',snap()['projects'][0]['workspaces'][1]['branch']=='feature/review')
    record('Retarget invalidates old result',snap()['projects'][0]['workspaces'][1]['stale'])
    page.keyboard.press('Control+k');record('Keyboard command palette',page.locator('#palette-search').is_visible());close();record('Escape closes dialog',page.locator('[role="dialog"]').count()==0)
    for view in ['operations','results','drive','system','work']:
        action('view',view);record('Navigation: '+view,snap()['view']==view)
    action('view','system');action('motion');record('Reduced motion setting',page.evaluate('document.documentElement.dataset.motion')=='off')
    action('fault');record('Runner fault is separate synthetic infrastructure state',snap()['fault']);action('fault');action('view','work')
    fresh();action('new-project');page.screenshot(path=str(OUT/'ui-new-project.png'),full_page=True)
    page.fill('#new-id','../escape');action('project-next');record('Project ID validation',page.locator('.errorline').count()>0)
    page.fill('#new-id','new-lab');page.fill('#new-name','Новая лаборатория');action('project-next');action('create-project')
    record('New project route admitted',snap()['project']=='new-lab' and snap()['ops'][0]['status']=='running')
    record('No fabricated test during provisioning',snap()['projects'][-1]['workspaces'][0]['run'] is None)
    page.wait_for_function("window.RUNNER_DEMO.snapshot().ops[0].status==='completed'",timeout=10000)
    record('No fabricated test after provisioning',snap()['projects'][-1]['workspaces'][0]['run'] is None)
    record('Empty result state',page.get_by_text('Проверки ещё не запускались').is_visible())
    action('new-workspace');page.fill('#ws-name','Вторая линия');page.fill('#ws-id','second');page.fill('#ws-branch','feature/second');action('create-workspace')
    page.wait_for_function("window.RUNNER_DEMO.snapshot().ops[0].status==='completed'",timeout=10000)
    record('Independent workspace creation',len(snap()['projects'][-1]['workspaces'])==2)
    fresh();before=snap()['projects'][0]['workspaces'][0].copy();action('new-operation');page.select_option('#operation-kind','verify');action('start-operation')
    record('Verify route has no push/PR', 'Push' not in page.locator('.route').inner_text())
    n=len(snap()['ops']);action('new-operation');action('start-operation');record('Busy operation is refused without queue',len(snap()['ops'])==n);close()
    page.wait_for_function("window.RUNNER_DEMO.snapshot().ops[0].status==='completed'",timeout=10000)
    after=snap()['projects'][0]['workspaces'][0]
    record('Verify preserves edited files and current branch head',after['dirty']==before['dirty'] and after['sha']==before['sha'])
    new_run=after['run'];action('new-operation');page.select_option('#operation-kind','publish');action('start-operation')
    record('Publish route has no test stage','Тесты' not in page.locator('.route').inner_text())
    page.wait_for_function("window.RUNNER_DEMO.snapshot().ops[0].status==='completed'",timeout=10000)
    record('Publish reuses run instead of fabricating tests',snap()['projects'][0]['workspaces'][0]['run']==new_run)
    n=len(snap()['ops']);page.wait_for_timeout(900);record('No automatic follow-up loop',len(snap()['ops'])==n and all(o['status']!='running' for o in snap()['ops']))
    action('new-operation');action('start-operation');action('cancel',snap()['ops'][0]['id']);record('Explicit cancellation',snap()['ops'][0]['status']=='cancelled')
    # Visual layout / responsiveness; no browser navigation required.
    fresh()
    for width,height in [(1280,800),(1600,1120),(2560,1440),(390,844)]:
        page.set_viewport_size({'width':width,'height':height});page.wait_for_timeout(100)
        dims=page.evaluate('({width:innerWidth,scroll:document.documentElement.scrollWidth})')
        record(f'No horizontal page overflow {width}px',dims['scroll']<=dims['width']+1)
    page.set_viewport_size({'width':1600,'height':1120})
    page.emulate_media(reduced_motion='reduce');record('System reduced-motion preference',page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches"))
    record('No browser JS errors',not errors);record('No external network requests',not requests)
    info={'browser':browser.version,'render_mode':'Chromium set_content; local navigation blocked by managed environment','checks':results,'count':len(results),'js_errors':errors,'network_requests':requests,'cloud_actions_tested':False}
    (OUT/'ui-tests.json').write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n')
    browser.close()
print(f'{len(results)} browser checks passed')
