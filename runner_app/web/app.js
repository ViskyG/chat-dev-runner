import {LocalApi, ApiError} from './api.js';
import {command, eligibility, isSha, isSlug, isUuid, latestRunOperation, retargetReason, runIdFrom, sourceHead} from './model.js';
import {renderApp, renderDialog} from './render.js';

const api = new LocalApi();
const root = document.getElementById('app');
const overlay = document.getElementById('overlay-root');
const toastRoot = document.getElementById('toast-root');
const state = {
  loading: true, authenticated: false, offline: false, error: '',
  system: null, projects: [], workspaces: {}, workspaceErrors: {}, workspaceConnections: {}, connectionReadErrors: {},
  operations: [], runs: {}, runErrors: {}, outputs: {}, outputErrors: {}, retention: {}, retentionReadAt: {},
  inspections: {}, inspectionErrors: {}, logErrorsOnly: false,
  partialOperations: false, partialRunEvidence: false,
  projectId: null, workspaceId: null, operationId: null,
  view: 'work', tab: 'summary', dialog: null, preview: null,
  theme: readPreference('runner-theme', 'dark'),
  motionOff: readPreference('runner-motion', 'off') === 'on',
};
let refreshPromise = null;
let focusReturn = null;
let toastTimer = null;

function readPreference(key, fallback) {
  try { return localStorage.getItem(key) || fallback; } catch (_) { return fallback; }
}
function writePreference(key, value) {
  try { localStorage.setItem(key, value); } catch (_) { /* preference is optional */ }
}
function errorText(error) {
  if (error instanceof ApiError) {
    if (error.code === 'BUSY') return 'BUSY: запрос не принят и не ждёт в очереди. Для повтора нужно новое явное действие.';
    if (error.code === 'RETENTION_NOT_DUE') return 'RETENTION_NOT_DUE: выводу ещё нет 14 дней; файл не удалён.';
    if (error.code === 'EVIDENCE_PINNED') return 'EVIDENCE_PINNED: запуск закреплён; файл не удалён.';
    if (error.code === 'EVIDENCE_REFERENCED') return 'EVIDENCE_REFERENCED: запуск нужен активной операции или доставке; файл не удалён.';
    if (error.code === 'EVIDENCE_EXPIRED') return 'EVIDENCE_EXPIRED: файл уже удалён; запись об очистке сохранена.';
    if (error.code === 'CONNECTION_UNAVAILABLE') return 'Локальный сервис недоступен. Сохранённые данные могут быть устаревшими.';
    if (error.status === 403 && error.code?.includes('SESSION')) return 'Локальная сессия истекла. Откройте её снова.';
    return error.message && error.message !== error.code ? `${error.code}: ${error.message}` : error.code;
  }
  return error instanceof Error ? error.message : 'Неизвестная ошибка.';
}
function toast(message) {
  toastRoot.replaceChildren();
  const node = document.createElement('div');
  node.className = 'toast';
  node.textContent = message;
  toastRoot.append(node);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.remove(), 7000);
}
function paint({focusDialog = false} = {}) {
  const pageScroll = [window.scrollX, window.scrollY];
  const scrollables = [...root.querySelectorAll('.tablewrap,.projectlist,.sidebar nav,.console')].map(node => [node.scrollLeft, node.scrollTop]);
  document.documentElement.dataset.theme = state.theme;
  document.documentElement.dataset.motion = state.motionOff ? 'off' : 'on';
  const previous = document.activeElement;
  const action = previous?.dataset?.action;
  const value = previous?.dataset?.value;
  root.innerHTML = renderApp(state);
  overlay.innerHTML = renderDialog(state);
  [...root.querySelectorAll('.tablewrap,.projectlist,.sidebar nav,.console')].forEach((node, index) => {
    if (scrollables[index]) [node.scrollLeft, node.scrollTop] = scrollables[index];
  });
  window.scrollTo(...pageScroll);
  if (focusDialog && state.dialog) {
    (overlay.querySelector('input,select,textarea') || overlay.querySelector('button'))?.focus();
  } else if (!state.dialog && action) {
    const buttons = [...root.querySelectorAll('[data-action]')];
    buttons.find(node => node.dataset.action === action && node.dataset.value === value)?.focus({preventScroll: true});
  }
}
function paintDialog({focus = false} = {}) {
  const previous = document.activeElement;
  const action = previous?.dataset?.action;
  const name = previous?.getAttribute?.('name');
  const id = previous?.id;
  overlay.innerHTML = renderDialog(state);
  if (!state.dialog) return;
  if (focus) (overlay.querySelector('input,select,textarea') || overlay.querySelector('button'))?.focus();
  else {
    const candidates = [...overlay.querySelectorAll('button,input,select,textarea,[tabindex]')];
    (candidates.find(node => action && node.dataset.action === action)
      || candidates.find(node => name && node.getAttribute('name') === name)
      || candidates.find(node => id && node.id === id)
      || overlay.querySelector('[data-action="close-dialog"]'))?.focus();
  }
}
function openDialog(type, extra = {}) {
  if (state.dialog?.type === 'output' && type !== 'output' && state.preview?.url) {
    URL.revokeObjectURL(state.preview.url); state.preview = null;
  }
  if (!state.dialog) focusReturn = document.activeElement;
  state.dialog = {type, error: '', ...extra};
  paintDialog({focus: true});
}
function closeDialog() {
  const repaintPage = state.dialog?.type === 'retention';
  const action = focusReturn?.dataset?.action;
  const value = focusReturn?.dataset?.value;
  const run = focusReturn?.dataset?.run;
  state.dialog = null;
  if (state.preview?.url) URL.revokeObjectURL(state.preview.url);
  state.preview = null;
  if (repaintPage) paint();
  else paintDialog();
  if (focusReturn?.isConnected) focusReturn.focus();
  else ([...root.querySelectorAll('[data-action]')].find(node =>
    node.dataset.action === action && node.dataset.value === value && node.dataset.run === run)
    || root.querySelector('[data-action="palette"]'))?.focus();
  focusReturn = null;
}
function dialogError(message) {
  if (!state.dialog) return;
  state.dialog.error = message;
  paintDialog();
  overlay.querySelector('[role="alert"]')?.focus();
}
function selectionActive() {
  return !!window.getSelection()?.toString();
}
function validBranch(value) {
  return typeof value === 'string' && value.length > 0 && value.length <= 1000
    && /^[A-Za-z0-9][A-Za-z0-9_./-]*$/.test(value)
    && !value.includes('..') && !value.includes('//') && !value.includes('@{')
    && !value.endsWith('/') && !value.endsWith('.') && !value.endsWith('.lock')
    && !value.split('/').some(part => part.startsWith('.'));
}
function validExportPath(value) {
  if (!value || value.length > 500 || value.startsWith('/') || /[\\:\x00-\x1f]/.test(value)) return false;
  const parts = value.split('/');
  if (parts.some(part => !part || part === '.' || part === '..')) return false;
  const blocked = new Set(['.git', '.ssh', '.aws', '.gnupg', '.netrc', 'secrets', '.runner-policy']);
  if (parts.some(part => blocked.has(part.toLowerCase()) || part.toLowerCase() === '.env' || part.toLowerCase().startsWith('.env.'))) return false;
  return !(parts[0] === '.github' && parts[1] === 'workflows');
}
function selectedWorkspace() {
  return state.workspaces[state.projectId]?.find(item => item.workspace_id === state.workspaceId) || null;
}
function selectedProject() { return state.projects.find(item => item.project_id === state.projectId) || null; }

async function limited(items, concurrency, callback) {
  let next = 0;
  const workers = Array.from({length: Math.min(concurrency, items.length)}, async () => {
    while (next < items.length) {
      const item = items[next++];
      try { await callback(item); } catch (_) { /* Missing individual evidence remains unknown. */ }
    }
  });
  await Promise.all(workers);
}
async function refresh({quiet = false} = {}) {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    try {
      const [system, projectPage, firstPage] = await Promise.all([api.system(), api.projects(), api.operations()]);
      if (!Array.isArray(projectPage.items) || !Array.isArray(firstPage.items)) throw new Error('Локальный API вернул неожиданный формат списка.');
      let operations = [...firstPage.items];
      let cursor = firstPage.next_cursor;
      let pages = 1;
      while (cursor && pages < 10) {
        const page = await api.operations(cursor);
        if (!Array.isArray(page.items)) throw new Error('Неполная страница операций.');
        operations.push(...page.items);
        cursor = page.next_cursor;
        pages += 1;
      }
      const workspaces = {}, workspaceErrors = {};
      await limited(projectPage.items, 6, async project => {
        if (!isSlug(project.project_id)) return;
        try {
          const page = await api.workspaces(project.project_id);
          if (!Array.isArray(page.items)) throw new Error('Неверный список областей.');
          workspaces[project.project_id] = page.items;
        } catch (error) { workspaceErrors[project.project_id] = errorText(error); }
      });
      const runs = {...state.runs}, runErrors = {};
      const outputs = {...state.outputs}, outputErrors = {};
      const runIds = [...new Set(operations.map(runIdFrom).filter(isUuid))];
      const selectedRun = latestRunOperation({operations}, state.projectId, state.workspaceId);
      const priority = runIdFrom(selectedRun);
      runIds.sort((a, b) => Number(b === priority) - Number(a === priority));
      await limited(runIds.slice(0, 100).filter(id => !runs[id] || !outputs[id]), 6, async id => {
        try { runs[id] = await api.run(id); }
        catch (error) { runErrors[id] = errorText(error); return; }
        try {
          const index = await api.outputs(id);
          if (!Array.isArray(index.items)) throw new Error('Неверный список выводов.');
          outputs[id] = index.items;
        } catch (error) { outputErrors[id] = errorText(error); }
      });
      state.system = system;
      state.projects = projectPage.items;
      state.workspaces = workspaces;
      state.workspaceErrors = workspaceErrors;
      state.operations = operations;
      state.runs = runs; state.runErrors = runErrors;
      state.outputs = outputs; state.outputErrors = outputErrors;
      state.partialOperations = !!cursor;
      state.partialRunEvidence = runIds.length > 100;
      state.offline = false;
      state.error = '';
      if (!state.projects.some(item => item.project_id === state.projectId)) {
        state.projectId = state.projects[0]?.project_id || null;
        state.workspaceId = null;
      }
      if (!state.workspaces[state.projectId]?.some(item => item.workspace_id === state.workspaceId)) {
        state.workspaceId = state.workspaces[state.projectId]?.[0]?.workspace_id || null;
      }
      await loadSelectedConnection();
      if (!state.dialog && (!quiet || !selectionActive())) paint();
    } catch (error) {
      state.offline = true;
      state.error = errorText(error);
      if (error instanceof ApiError && error.status === 403) {
        state.authenticated = false;
        api.csrf = null;
      }
      if (!quiet) toast(state.error);
      if (!state.dialog && (!quiet || !selectionActive())) paint();
    } finally {
      refreshPromise = null;
    }
  })();
  return refreshPromise;
}

async function loadSelectedConnection() {
  if (!isSlug(state.projectId) || !isSlug(state.workspaceId)) return;
  const key = `${state.projectId}/${state.workspaceId}`;
  try {
    state.workspaceConnections[key] = await api.workspaceConnection(state.projectId, state.workspaceId);
    delete state.connectionReadErrors[key];
  } catch (error) { state.connectionReadErrors[key] = errorText(error); }
}

async function submitAction(action, projectId, workspaceId, expectedHead, params, intent) {
  const denied = eligibility(state, action, projectId, action === 'CREATE_WORKSPACE' ? null : workspaceId);
  if (denied) throw new Error(denied);
  const payload = command(state.system, action, projectId, workspaceId, expectedHead, params, intent);
  const receipt = await api.submit(payload);
  closeDialog();
  state.operationId = payload.request_id;
  state.view = 'operations';
  toast(receipt.status === 'RUNNING' || receipt.status === 'ACCEPTED'
    ? `Запрос ${payload.request_id} принят. Следующее действие не запустится само.`
    : `Запрос ${payload.request_id}: ${receipt.status || 'ответ получен'}.`);
  await refresh();
}

async function login(form) {
  const input = form.elements.namedItem('bootstrap') || form.querySelector('#bootstrap-input');
  const value = input?.value.trim();
  if (input) input.value = '';
  if (!value) { state.error = 'Введите одноразовый локальный код.'; paint(); return; }
  try {
    await api.login(value);
    state.authenticated = true;
    state.error = '';
    await refresh();
  } catch (error) {
    state.error = errorText(error);
    paint();
  }
}

async function projectPlan(form) {
  const data = new FormData(form);
  const draft = Object.fromEntries([...data].map(([key, value]) => [key, String(value).trim()]));
  if (!draft.name || draft.name.length > 80 || !isSlug(draft.id) || !isSlug(draft.templateId) || !isSlug(draft.githubIdentity)) {
    dialogError('Проверьте название, постоянный ID, шаблон и GitHub identity. ID содержит a–z, 0–9 или дефис.');
    return;
  }
  if (!state.system?.approved?.templates?.includes(draft.templateId)
      || !state.system?.approved?.github_identities?.includes(draft.githubIdentity)) {
    dialogError('Шаблон или GitHub identity не входят в локально утверждённый список.'); return;
  }
  if (state.projects.some(p => p.project_id === draft.id)) { dialogError('Проект с таким ID уже есть.'); return; }
  if (draft.sourceKind === 'github' && (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(draft.repository)
      || !state.system?.approved?.github_repositories?.includes(draft.repository))) {
    dialogError('Для GitHub укажите owner/repository.'); return;
  }
  if (draft.sourceKind === 'local' && (!draft.localPath.startsWith('/') || draft.localPath.includes('\0'))) {
    dialogError('Для локального Git укажите абсолютный путь на этой машине.'); return;
  }
  if (draft.sourceKind !== 'github') draft.repository = '';
  if (draft.sourceKind !== 'local') draft.localPath = '';
  state.dialog = {type: 'new-project', step: 2, draft, error: ''};
  paintDialog({focus: true});
}
async function createProject() {
  const draft = state.dialog?.draft;
  if (!draft) return;
  try {
    let source = {kind: 'new'};
    if (draft.sourceKind === 'github') source = {kind: 'github', repository: draft.repository};
    if (draft.sourceKind === 'local') {
      const grant = await api.importHandle(draft.localPath);
      if (!isSlug(grant.import_handle)) throw new Error('Локальный импорт не вернул допустимый handle.');
      source = {kind: 'local', import_handle: grant.import_handle};
    }
    await submitAction('CREATE_PROJECT', draft.id, null, null, {
      name: draft.name, source, template_id: draft.templateId,
      github_identity: draft.githubIdentity, visibility: 'private',
    }, `Создать приватный проект ${draft.name}`);
  } catch (error) { dialogError(errorText(error)); await refresh({quiet: true}); }
}
async function createWorkspace(form) {
  const data = new FormData(form);
  const name = String(data.get('name') || '').trim();
  const id = String(data.get('id') || '').trim();
  const branch = String(data.get('branch') || '').trim();
  const baseCommit = String(data.get('baseCommit') || '').trim();
  const profileId = String(data.get('profileId') || '').trim();
  if (!name || name.length > 80 || !isSlug(id) || !isSha(baseCommit) || !isSlug(profileId)
      || !validBranch(branch)) {
    dialogError('Проверьте название, ID, новую ветку, полный SHA и разрешённый профиль.'); return;
  }
  if (!state.system?.approved?.profiles?.includes(profileId)) {
    dialogError('Профиль не входит в локально утверждённый список.'); return;
  }
  if (state.workspaces[state.projectId]?.some(w => w.workspace_id === id || w.branch === branch)) {
    dialogError('ID или ветка уже используются в этом проекте.'); return;
  }
  try {
    await submitAction('CREATE_WORKSPACE', state.projectId, id, baseCommit,
      {name, branch, base_commit: baseCommit, profile_id: profileId}, `Создать область ${name}`);
  } catch (error) { dialogError(errorText(error)); await refresh({quiet: true}); }
}
async function submitOperation(form) {
  const workspace = selectedWorkspace();
  if (!workspace) { dialogError('Выберите рабочую область.'); return; }
  const data = new FormData(form);
  const action = String(data.get('action'));
  const profileId = String(data.get('profileId') || '').trim();
  const sourceRunId = String(data.get('sourceRunId') || '').trim();
  const publication = String(data.get('publication'));
  const intent = String(data.get('intent') || '').trim();
  const head = sourceHead(workspace);
  if (!intent || intent.length > 1200) { dialogError('Укажите цель операции до 1200 символов.'); return; }
  let params = {};
  let expectedHead = null;
  if (action === 'VERIFY') {
    if (!isSlug(profileId) || !state.system?.approved?.profiles?.includes(profileId) || !isSha(head)) {
      dialogError('Для проверки нужен утверждённый профиль и записанный полный HEAD.'); return;
    }
    params = {profile_id: profileId}; expectedHead = head;
  } else if (action === 'PUBLISH') {
    if (!isUuid(sourceRunId) || !isSha(head)) { dialogError('Для публикации нужен сохранённый run ID и текущий полный HEAD.'); return; }
    params = {source_run_id: sourceRunId, publication}; expectedHead = head;
  } else if (action === 'PUBLISH_REPORT') {
    if (!isUuid(sourceRunId)) { dialogError('Для доставки нужен сохранённый run ID. Тесты не повторяются.'); return; }
    params = {source_run_id: sourceRunId};
  } else { dialogError('Этот маршрут пока не подключён в форме.'); return; }
  try { await submitAction(action, state.projectId, state.workspaceId, expectedHead, params, intent); }
  catch (error) { dialogError(errorText(error)); await refresh({quiet: true}); }
}

async function openSourceEditor() {
  const projectId = state.projectId, workspaceId = state.workspaceId;
  if (!isSlug(projectId) || !isSlug(workspaceId)) return;
  if (state.dialog?.type !== 'workspace-edit') openDialog('workspace-edit', {loading: true, source: null});
  else { state.dialog.loading = true; state.dialog.error = ''; paintDialog(); }
  try {
    const source = await api.workspaceSource(projectId, workspaceId);
    if (state.dialog?.type === 'workspace-edit' && projectId === state.projectId && workspaceId === state.workspaceId) {
      state.dialog.loading = false; state.dialog.source = source; paintDialog();
    }
  } catch (error) {
    if (state.dialog?.type === 'workspace-edit') { state.dialog.loading = false; dialogError(errorText(error)); }
  }
}
async function retargetWorkspace(form) {
  const source = state.dialog?.source;
  const why = retargetReason(state, source, state.projectId, state.workspaceId);
  if (why) { dialogError(why); return; }
  const data = new FormData(form);
  const targetBranch = String(data.get('targetBranch') || '').trim();
  const targetCommit = String(data.get('targetCommit') || '').trim();
  if (!validBranch(targetBranch) || !isSha(targetCommit)) {
    dialogError('Укажите корректную целевую ветку и полный commit SHA.'); return;
  }
  if (targetBranch === source.recorded_branch && targetCommit === source.recorded_head) {
    dialogError('Это уже текущая ветка и commit. Изменений нет.'); return;
  }
  if (state.workspaces[state.projectId]?.some(item => item.workspace_id !== state.workspaceId && item.branch === targetBranch)) {
    dialogError('Эта ветка уже занята другой рабочей областью.'); return;
  }
  try { await submitAction('RETARGET_WORKSPACE', state.projectId, state.workspaceId, source.recorded_head,
    {expected_branch: source.recorded_branch, target_branch: targetBranch, target_commit: targetCommit},
    `Сменить ветку ${state.projectId}/${state.workspaceId} на ${targetBranch}`); }
  catch (error) { dialogError(errorText(error)); await refresh({quiet: true}); }
}

async function exportContext(form) {
  const workspace = selectedWorkspace();
  const raw = String(new FormData(form).get('paths') || '');
  const paths = raw.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
  const head = sourceHead(workspace);
  if (!workspace || !isSha(head) || !paths.length || paths.length > 100
      || new Set(paths).size !== paths.length || paths.some(path => !validExportPath(path))) {
    dialogError('Нужны 1–100 разных относительных безопасных путей и подтверждённый HEAD области.'); return;
  }
  const includeDiff = false; // No pinned committed diff basis exists in v1.
  try { await submitAction('EXPORT_CONTEXT', state.projectId, state.workspaceId, head,
    {paths, include_diff: includeDiff}, `Выдать контекст ${state.projectId}/${state.workspaceId}`); }
  catch (error) { dialogError(errorText(error)); await refresh({quiet: true}); }
}

async function loadRetention(runId) {
  if (!isUuid(runId)) throw new Error('Неверный ID запуска.');
  const value = await api.retention(runId);
  if (value.run_id !== runId || typeof value.pinned !== 'boolean' || !Array.isArray(value.outputs)) {
    throw new Error('Локальный API вернул неверный статус хранения.');
  }
  state.retention[runId] = value;
  state.retentionReadAt[runId] = new Date().toISOString();
  return value;
}
async function openRetention(runId) {
  if (!isUuid(runId)) { toast('Неверный ID запуска.'); return; }
  openDialog('retention', {runId, loading: true, busy: false, confirmOutputId: null});
  try {
    await loadRetention(runId);
    if (state.dialog?.type === 'retention' && state.dialog.runId === runId) {
      state.dialog.loading = false; paintDialog();
    }
  } catch (error) {
    if (state.dialog?.type === 'retention' && state.dialog.runId === runId) {
      delete state.retention[runId]; delete state.retentionReadAt[runId];
      state.dialog.loading = false; dialogError(errorText(error));
    }
  }
}
async function refreshRetention() {
  const dialog = state.dialog;
  if (dialog?.type !== 'retention' || dialog.busy) return;
  dialog.loading = true; dialog.error = ''; dialog.confirmOutputId = null; paintDialog();
  try { await loadRetention(dialog.runId); dialog.loading = false; paintDialog(); }
  catch (error) {
    delete state.retention[dialog.runId]; delete state.retentionReadAt[dialog.runId];
    dialog.loading = false; dialogError(errorText(error));
  }
}
async function pinRun() {
  const dialog = state.dialog;
  if (dialog?.type !== 'retention' || dialog.busy) return;
  const status = state.retention[dialog.runId];
  if (!status) return;
  dialog.busy = true; dialog.error = ''; paintDialog();
  try {
    const next = await api.pinRun(dialog.runId, !status.pinned);
    if (next.run_id !== dialog.runId || typeof next.pinned !== 'boolean' || !Array.isArray(next.outputs)) {
      throw new Error('Локальный API вернул неверный статус закрепления.');
    }
    state.retention[dialog.runId] = next;
    state.retentionReadAt[dialog.runId] = new Date().toISOString();
    toast(next.pinned ? 'Запуск закреплён.' : 'Закрепление снято.');
  } catch (error) { dialog.error = errorText(error); }
  finally { dialog.busy = false; if (state.dialog === dialog) paintDialog(); }
}
async function expireOutput() {
  const dialog = state.dialog;
  if (dialog?.type !== 'retention' || dialog.busy) return;
  const status = state.retention[dialog.runId];
  const outputId = dialog.confirmOutputId;
  if (!isSlug(outputId) || status?.pinned || !status?.outputs?.some(item => item.output_id === outputId && item.state === 'AVAILABLE')) {
    dialogError('Этот вывод нельзя очистить по текущему статусу хранения.'); return;
  }
  dialog.busy = true; dialog.error = ''; paintDialog();
  try {
    await api.expireOutput(dialog.runId, outputId);
    await loadRetention(dialog.runId);
    dialog.confirmOutputId = null;
    toast(`Вывод ${outputId} очищен; запись об удалении сохранена.`);
  } catch (error) { dialog.error = errorText(error); }
  finally { dialog.busy = false; if (state.dialog === dialog) paintDialog(); }
}
async function loadTabInspections() {
  if (state.tab !== 'tests' && state.tab !== 'console') return;
  const projectId = state.projectId, workspaceId = state.workspaceId, tab = state.tab;
  const operation = latestRunOperation(state, projectId, workspaceId);
  const runId = runIdFrom(operation);
  if (!runId) return;
  const kind = tab === 'tests' ? 'test_result' : 'application_log';
  const outputs = (state.outputs[runId] || []).filter(item => item.kind === kind);
  await limited(outputs, 3, async item => {
    const key = `${runId}/${item.output_id}`;
    if (state.inspections[key] || state.inspectionErrors[key]) return;
    try { state.inspections[key] = await api.inspectOutput(runId, item.output_id); }
    catch (error) { state.inspectionErrors[key] = errorText(error); }
  });
  if (!state.dialog && state.projectId === projectId && state.workspaceId === workspaceId
      && state.tab === tab) paint();
}
async function inspectOutput(runId, outputId) {
  if (!isUuid(runId) || !isSlug(outputId)) return;
  const output = (state.outputs[runId] || []).find(item => item.output_id === outputId);
  if (!output) return;
  state.preview = {loading: true, text: null, error: null};
  openDialog('output', {runId, outputId});
  try { await loadRetention(runId); } catch (_) {
    delete state.retention[runId]; delete state.retentionReadAt[runId];
    /* Preview still has a direct read result. */
  }
  if (state.dialog?.type !== 'output' || state.dialog.runId !== runId || state.dialog.outputId !== outputId) return;
  if (state.retention[runId]?.outputs?.find(item => item.output_id === outputId)?.state === 'EXPIRED') {
    state.preview = {loading: false, text: null, error: 'Вывод удалён; сохранена запись об очистке.'};
    if (state.dialog?.type === 'output') paintDialog();
    return;
  }
  const type = String(output.media_type || '').toLowerCase();
  const textual = type.startsWith('text/') || ['application/json', 'application/xml', 'application/x-ndjson', 'image/svg+xml'].includes(type);
  if (output.kind === 'screenshot' && ['image/png', 'image/jpeg', 'image/webp'].includes(type)) {
    try {
      const inspection = await api.inspectOutput(runId, outputId);
      state.inspections[`${runId}/${outputId}`] = inspection;
      if (!inspection.safe_preview || !['image/png', 'image/jpeg', 'image/webp'].includes(inspection.preview_media_type))
        throw new Error('Безопасный предпросмотр этого изображения недоступен; можно скачать исходный файл.');
      const blob = await api.output(runId, outputId);
      if (blob.size !== output.size_bytes || blob.size > 2097152)
        throw new Error('Размер изображения не совпал с зарегистрированным выводом.');
      const url = URL.createObjectURL(new Blob([blob], {type: inspection.preview_media_type}));
      if (state.dialog?.type !== 'output' || state.dialog.runId !== runId || state.dialog.outputId !== outputId) {
        URL.revokeObjectURL(url); return;
      }
      state.preview = {loading: false, text: null, error: null, url,
        width: inspection.width, height: inspection.height};
    } catch (error) { state.preview = {loading: false, text: null, error: errorText(error)}; }
  } else if (!textual) state.preview = {loading: false, text: null, error: 'Для этого формата доступно только скачивание.'};
  else if (output.size_bytes > 262144) state.preview = {loading: false, text: null, error: 'Файл больше 256 КБ. Скачайте его для просмотра.'};
  else {
    try {
      const blob = await api.output(runId, outputId);
      if (blob.size > 262144) throw new Error('Файл превышает предел предпросмотра.');
      const text = new TextDecoder('utf-8', {fatal: true}).decode(await blob.arrayBuffer());
      state.preview = {loading: false, text, error: null};
    } catch (error) { state.preview = {loading: false, text: null, error: errorText(error)}; }
  }
  if (state.dialog?.type === 'output' && state.dialog.runId === runId && state.dialog.outputId === outputId) {
    paintDialog();
    overlay.querySelector('#output-preview')?.focus();
  }
}
function downloadOutput() {
  const {runId, outputId} = state.dialog || {};
  if (!isUuid(runId) || !isSlug(outputId)) return;
  const link = document.createElement('a');
  link.href = `/api/v1/runs/${runId}/outputs/${outputId}`;
  link.download = `${outputId}.bin`;
  link.rel = 'noopener';
  document.body.append(link);
  link.click();
  link.remove();
}
async function localAction(action, projectId, workspaceId, expectedHead, params, intent) {
  try { await submitAction(action, projectId, workspaceId, expectedHead, params, intent); }
  catch (error) { toast(errorText(error)); await refresh({quiet: true}); }
}

root.addEventListener('click', async event => {
  const element = event.target.closest('[data-action]');
  if (!element || element.disabled) return;
  const action = element.dataset.action;
  const value = element.dataset.value;
  if (action === 'view') { state.view = value; state.operationId = null; paint(); }
  else if (action === 'project') {
    state.projectId = value;
    state.workspaceId = state.workspaces[value]?.[0]?.workspace_id || null;
    state.view = 'work'; state.tab = 'summary'; paint(); void loadSelectedConnection().then(() => { if (!state.dialog) paint(); });
  } else if (action === 'workspace') { state.workspaceId = value; state.tab = 'summary'; paint(); void loadSelectedConnection().then(() => { if (!state.dialog) paint(); }); }
  else if (action === 'tab') { state.tab = value; paint(); void loadTabInspections(); }
  else if (action === 'log-errors-only') { state.logErrorsOnly = !state.logErrorsOnly; paint(); }
  else if (action === 'operation') { state.operationId = value; state.view = 'operations'; paint(); }
  else if (action === 'theme') { state.theme = state.theme === 'dark' ? 'light' : 'dark'; writePreference('runner-theme', state.theme); paint(); }
  else if (action === 'motion') { state.motionOff = !state.motionOff; writePreference('runner-motion', state.motionOff ? 'on' : 'off'); paint(); }
  else if (action === 'palette') openDialog('palette');
  else if (action === 'refresh') await refresh();
  else if (action === 'new-project') openDialog('new-project', {step: 1, draft: {sourceKind: 'new'}});
  else if (action === 'new-workspace') openDialog('new-workspace');
  else if (action === 'operation-modal' || action === 'verify') openDialog('operation');
  else if (action === 'code-menu') openDialog('code');
  else if (action === 'export-context') openDialog('export-context');
  else if (action === 'workspace-edit') await openSourceEditor();
  else if (action === 'workspace-drive') {
    const projectId = state.projectId, workspaceId = state.workspaceId;
    openDialog('drive-docs', {loading: true, docs: null});
    try {
      const docs = await api.driveDocs(projectId, workspaceId);
      if (state.dialog?.type === 'drive-docs' && projectId === state.projectId && workspaceId === state.workspaceId) {
        state.dialog.loading = false; state.dialog.docs = docs; paintDialog();
      }
    } catch (error) {
      if (state.dialog?.type === 'drive-docs') { state.dialog.loading = false; dialogError(errorText(error)); }
    }
  }
  else if (action === 'check-drive' || action === 'check-github' || action === 'check-git-remote') {
    try {
      const check = action === 'check-drive' ? await api.checkDrive()
        : action === 'check-github' ? await api.checkGithub()
        : await api.checkGitRemote(state.projectId, state.workspaceId);
      await refresh({quiet: true});
      toast(check.status === 'OK' ? 'Проверка завершена: доступ подтверждён.'
        : `Проверка завершена с ошибкой ${check.code || 'UNKNOWN'}.`);
    } catch (error) { toast(errorText(error)); }
  }
  else if (action === 'workspace-editor') {
    try { await api.openEditor(state.projectId, state.workspaceId); toast('VS Code: запрос на открытие области передан локальному runner.'); }
    catch (error) { toast(errorText(error)); }
  }
  else if (action === 'output') await inspectOutput(element.dataset.run, value);
  else if (action === 'retention') await openRetention(element.dataset.run);
  else if (action === 'fetch') await localAction('FETCH', state.projectId, null, null, {}, `Обновить отслеживаемые ветки Git для ${state.projectId}`);
  else if (action === 'arm' || action === 'disarm') {
    try { await api[action](); await refresh(); toast(action === 'arm' ? 'Приём команд включён. Эпоха обновлена.' : 'Приём новых команд выключен.'); }
    catch (error) { toast(errorText(error)); }
  } else if (action === 'cancel') {
    const op = state.operations.find(item => item.request_id === state.operationId);
    if (op && op.status === 'RUNNING') await localAction('CANCEL_OPERATION', op.project_id, op.workspace_id, null,
      {target_request_id: op.request_id}, `Остановить операцию ${op.request_id}`);
  } else if (action === 'download-operation') {
    const operation = state.operations.find(item => item.request_id === state.operationId);
    if (!operation) return;
    const blob = new Blob([JSON.stringify(operation, null, 2)], {type: 'application/json'});
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url; link.download = `${operation.request_id}.json`;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } else if (action === 'run') {
    const op = state.operations.find(item => item.request_id === state.operationId);
    const runId = element.dataset.run || runIdFrom(op);
    const run = state.runs[runId];
    if (run) {
      state.projectId = run.project_id;
      state.workspaceId = run.workspace_id;
      state.view = 'work'; state.tab = 'artifacts'; paint();
    } else toast('Сохранённый запуск недоступен в текущей проекции. Обновите данные.');
  }
});
overlay.addEventListener('click', async event => {
  const element = event.target.closest('[data-action]');
  if (!element || element.disabled) return;
  const action = element.dataset.action;
  if (action === 'backdrop' && event.target === element) closeDialog();
  else if (action === 'close-dialog') closeDialog();
  else if (action === 'palette-view') { closeDialog(); state.view = element.dataset.value; paint(); }
  else if (action === 'palette-workspace') {
    closeDialog(); state.projectId = element.dataset.project; state.workspaceId = element.dataset.value;
    state.view = 'work'; paint();
  } else if (action === 'project-back') { state.dialog.step = 1; paintDialog({focus: true}); }
  else if (action === 'project-confirm') await createProject();
  else if (action === 'refresh-source') await openSourceEditor();
  else if (action === 'open-editor') {
    try {
      await api.openEditor(state.projectId, element.dataset.value);
      closeDialog(); toast('VS Code: запрос на открытие выбранной области передан локальному runner.');
    } catch (error) { dialogError(errorText(error)); }
  }
  else if (action === 'retention-from-output') await openRetention(state.dialog?.runId);
  else if (action === 'refresh-retention') await refreshRetention();
  else if (action === 'pin-run') await pinRun();
  else if (action === 'expire-output') {
    if (state.dialog?.type === 'retention') {
      state.dialog.confirmOutputId = element.dataset.value;
      state.dialog.error = '';
      paintDialog();
      overlay.querySelector('[data-action="expire-output-confirm"]')?.focus();
    }
  }
  else if (action === 'expire-output-cancel') {
    if (state.dialog?.type === 'retention') { state.dialog.confirmOutputId = null; paintDialog(); }
  }
  else if (action === 'expire-output-confirm') await expireOutput();
  else if (action === 'download-output') downloadOutput();
  else if (action === 'copy-output') {
    if (state.preview?.text) {
      try { await navigator.clipboard.writeText(state.preview.text); toast('Текст скопирован.'); }
      catch (_) { toast('Буфер обмена недоступен; выделите текст вручную.'); }
    }
  }
});
document.addEventListener('submit', async event => {
  const form = event.target;
  const formId = form.getAttribute('id');
  if (!['login-form', 'project-form', 'workspace-form', 'operation-form', 'export-form', 'retarget-form'].includes(formId)) return;
  event.preventDefault();
  if (formId === 'login-form') await login(form);
  else if (formId === 'project-form') await projectPlan(form);
  else if (formId === 'workspace-form') await createWorkspace(form);
  else if (formId === 'operation-form') await submitOperation(form);
  else if (formId === 'export-form') await exportContext(form);
  else if (formId === 'retarget-form') await retargetWorkspace(form);
});
overlay.addEventListener('input', event => {
  if (event.target.id !== 'palette-search') return;
  const query = event.target.value.toLocaleLowerCase('ru-RU');
  overlay.querySelectorAll('#palette-items .menu-item').forEach(item => { item.hidden = !item.textContent.toLocaleLowerCase('ru-RU').includes(query); });
});
document.addEventListener('keydown', event => {
  if (event.target?.matches?.('[role=\"tab\"]') && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
    event.preventDefault();
    const tabs = [...root.querySelectorAll('[role=\"tab\"]')];
    const at = tabs.indexOf(event.target);
    if (at >= 0) {
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
        : (at + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
      state.tab = tabs[next].dataset.value; paint();
      root.querySelector(`[role=\"tab\"][data-value=\"${state.tab}\"]`)?.focus();
      void loadTabInspections();
    }
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    if (state.authenticated) openDialog('palette');
    return;
  }
  if (!state.dialog) return;
  if (event.key === 'Escape') { event.preventDefault(); closeDialog(); return; }
  if (event.key !== 'Tab') return;
  const focusables = [...overlay.querySelectorAll('button:not(:disabled):not([hidden]),input:not(:disabled):not([hidden]),select:not(:disabled):not([hidden]),textarea:not(:disabled):not([hidden]),a[href],[tabindex="0"]')]
    .filter(node => !node.closest('[hidden]'));
  if (!focusables.length) return;
  const first = focusables[0], last = focusables[focusables.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});

async function boot() {
  try {
    await api.resume();
    state.authenticated = true;
    state.loading = false;
    await refresh();
  } catch (error) {
    state.loading = false;
    state.authenticated = false;
    state.error = error instanceof ApiError && error.status === 403 ? '' : errorText(error);
    paint();
  }
}
setInterval(() => {
  if (state.authenticated && !document.hidden && !state.dialog) void refresh({quiet: true});
}, 15000);
void boot();
