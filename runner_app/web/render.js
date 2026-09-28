import {
  actionLabel, eligibility, freshCheck, label, latestRunOperation, projectOperations,
  retargetReason, runIdFrom, short, snapshotState, sourceHead, sortedOperations, timeText, tone,
  workspaceOperations,
} from './model.js';

// Every interpolated value is escaped. Output bytes are inserted with textContent by app.js.
export const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[char]);
const text = value => esc(value || '—');
const badge = (value, override) => `<span class="badge ${esc(override || tone(value))}">${esc(label(value))}</span>`;
const deliveryLabel = value => value === 'FAILED' ? 'Не доставлено' : label(value);
const deliveryBadge = value => `<span class="badge ${esc(tone(value))}">${esc(deliveryLabel(value))}</span>`;
const button = (action, caption, reason = null, extra = '') =>
  `<button type="button" class="btn ${extra}" data-action="${action}" ${reason ? `disabled title="${esc(reason)}" aria-description="${esc(reason)}"` : ''}>${esc(caption)}</button>`;
const reason = why => why ? `<span class="disabled-reason">${esc(why)}</span>` : '';
const row = (name, value) => `<div class="definition"><strong>${esc(name)}</strong><span>${text(value)}</span></div>`;
const notice = message => `<p class="run-note">${esc(message)}</p>`;
function githubPrLink(publication) {
  const value = publication?.pull_request_url;
  if (typeof value !== 'string') return '';
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.hostname !== 'github.com'
        || !/^\/[A-Za-z0-9-]+\/[A-Za-z0-9._-]+\/pull\/[0-9]+$/.test(url.pathname)) return '';
    return `<a class="btn small" href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">Открыть черновик PR</a>`;
  } catch (_) { return ''; }
}
const empty = (title, detail = '') => `<div class="empty-state"><strong>${esc(title)}</strong><p>${esc(detail)}</p></div>`;

function currentProject(state) { return state.projects.find(p => p.project_id === state.projectId) || null; }
function currentWorkspace(state) { return state.workspaces[state.projectId]?.find(w => w.workspace_id === state.workspaceId) || null; }
function currentRun(state, workspace) {
  const operation = workspace && latestRunOperation(state, state.projectId, workspace.workspace_id);
  return operation ? state.runs[runIdFrom(operation)] || null : null;
}

function connectionStrip(state) {
  const checks = state.system?.connections || {};
  const key = `${state.projectId}/${state.workspaceId}`;
  const gitRemote = state.workspaceConnections?.[key]?.git_remote;
  const gitRemoteCheck = freshCheck(gitRemote);
  const gitFetchCheck = freshCheck(state.workspaceConnections?.[key]?.git_fetch);
  if (state.connectionReadErrors?.[key]) {
    for (const check of [gitRemoteCheck, gitFetchCheck]) {
      check.text = `Локальная проекция недоступна · ${check.text}`;
      check.tone = 'warn';
    }
  }
  if (gitRemote?.workspace_snapshot_current === false) {
    gitRemoteCheck.text = `Устарело: состояние области изменилось · ${gitRemoteCheck.text}`;
    gitRemoteCheck.tone = 'warn';
  }
  const items = [
    ['Drive · доступ', freshCheck(checks.drive)],
    ['Git · fetch', gitFetchCheck],
    ['Git · remote', gitRemoteCheck],
    ['GitHub · API identity', freshCheck(checks.github_api)],
  ];
  if (state.offline) items.forEach(([, check]) => {
    check.text = `Runner недоступен · ${check.text}`; check.tone = 'warn';
  });
  return `<section class="healthstrip" data-testid="connection-strip" aria-label="Свежесть подключений">
    <span class="healthitem"><span class="dot ${state.offline ? 'red' : 'gray'}"></span>Локальный runner · ${state.offline ? 'недоступен' : state.system?.armed ? 'приём включён' : 'приём выключен'}</span>
    ${items.map(([name, check]) => `<span class="healthitem"><span class="dot ${check.tone === 'ok' ? '' : check.tone === 'warn' ? 'amber' : 'gray'}"></span>${esc(name)} · ${esc(check.text)}</span>`).join('')}
  </section>`;
}

function sidebar(state) {
  const nav = [['work', 'Рабочие области'], ['operations', 'Операции'], ['results', 'Результаты'], ['drive', 'Google Drive'], ['system', 'Система']];
  return `<aside class="sidebar"><div class="brand"><span class="brandmark" aria-hidden="true"></span><span><strong>Runner</strong><small>Мастерская разработки</small></span></div>
    <nav aria-label="Разделы">${nav.map(([id, title]) => `<button type="button" class="navitem ${state.view === id ? 'on' : ''}" data-action="view" data-value="${id}" ${state.view === id ? 'aria-current="page"' : ''}>${title}</button>`).join('')}</nav>
    <div class="sidecap">Проекты <span>${state.projects.length}</span></div>
    <div class="projectlist" data-testid="project-list">${state.projects.map(project => `<button type="button" class="projectpick ${project.project_id === state.projectId ? 'on' : ''}" data-action="project" data-value="${esc(project.project_id)}"><span class="projecticon" aria-hidden="true">${esc(project.display_name?.slice(0, 1) || 'P')}</span><span class="projectlabel">${esc(project.display_name || project.project_id)}</span></button>`).join('') || '<p class="side-note">Проектов пока нет.</p>'}</div>
    ${button('new-project', '＋ Новый проект', eligibility(state, 'CREATE_PROJECT'), 'newproject')}
    <div class="sidefoot"><div class="local"><span class="dot ${state.offline ? 'red' : 'gray'}"></span>Только на этом компьютере</div><span>Один запрос — одна конечная операция. Очереди нет.</span></div>
  </aside>`;
}

function toolbar(state) {
  const project = currentProject(state);
  return `<header class="topbar"><div class="crumb">Мастерская / <strong>${esc(project?.display_name || state.viewTitle)}</strong></div><div class="topcontrols">
    <button type="button" class="cmdkey" data-action="palette" data-testid="palette-open">Перейти к… <span class="kbd">Ctrl K</span></button>
    <button type="button" class="iconbtn" data-action="refresh" aria-label="Обновить реальные данные" title="Обновить реальные данные">↻</button>
    <button type="button" class="iconbtn" data-action="theme" data-testid="theme-toggle" aria-label="Сменить тему" title="Сменить тему">${state.theme === 'dark' ? '☼' : '☾'}</button>
  </div></header>`;
}

function workspaceCard(state, workspace) {
  const operation = latestRunOperation(state, state.projectId, workspace.workspace_id);
  const run = operation && state.runs[runIdFrom(operation)];
  const head = sourceHead(workspace);
  return `<button type="button" class="workspace ${workspace.workspace_id === state.workspaceId ? 'selected' : ''}" data-action="workspace" data-value="${esc(workspace.workspace_id)}" aria-pressed="${workspace.workspace_id === state.workspaceId}">
    <span class="ws-top"><strong>${esc(workspace.display_name || workspace.workspace_id)}</strong>${badge(workspace.state)}</span>
    <span class="ws-sub">${esc(workspace.branch || 'Ветка не записана')}</span>
    <span class="ws-dirty">HEAD ${head ? esc(short(head)) : 'неизвестен'} · изменения файлов не проверены</span>
    <span class="ws-last">${run ? `Запуск ${esc(short(run.run_id, 8))} · ${esc(timeText(run.created_at))}` : 'Запусков нет'}</span>
  </button>`;
}

function retentionOutput(state, runId, outputId) {
  return state.retention?.[runId]?.outputs?.find(item => item.output_id === outputId) || null;
}
function retentionSummary(state, runId) {
  const status = state.retention?.[runId];
  if (!status) return 'Хранение не проверено';
  return `${status.pinned ? 'Закреплён владельцем' : 'Не закреплён'} · проверено ${timeText(state.retentionReadAt?.[runId])}`;
}
function outputButton(state, runId, output, className = 'btn small') {
  const retention = retentionOutput(state, runId, output.output_id);
  const expired = retention?.state === 'EXPIRED';
  const why = 'Вывод удалён; сохранена запись об очистке.';
  return `<button type="button" class="${className}" data-action="output" data-value="${esc(output.output_id)}" data-run="${esc(runId)}" ${expired ? `disabled title="${esc(why)}" aria-description="${esc(why)}"` : ''}>${esc(output.output_id)}${expired ? ' · удалён' : ''}</button>`;
}
function outputList(state, run) {
  const outputs = state.outputs[run.run_id];
  if (outputs === undefined) return state.outputErrors[run.run_id]
    ? empty('Выводы недоступны', state.outputErrors[run.run_id])
    : empty('Список выводов загружается');
  if (!outputs.length) return empty('Выводов нет', 'Запуск сохранён без зарегистрированных файлов вывода.');
  return `<div class="artifact-list" data-testid="output-list">${outputs.map(output => {
    const expired = retentionOutput(state, run.run_id, output.output_id)?.state === 'EXPIRED';
    return `<div class="artifact-row">${outputButton(state, run.run_id, output, 'artifact')}<small>${esc(output.kind)} · ${esc(output.format)} · ${Number(output.size_bytes || 0).toLocaleString('ru-RU')} байт · ${expired ? 'запись об удалении' : output.complete ? 'полный' : 'неполный'}</small></div>`;
  }).join('')}</div>`;
}

function runPanel(state, workspace) {
  const operation = latestRunOperation(state, state.projectId, workspace.workspace_id);
  const run = currentRun(state, workspace);
  if (!operation) return `<div class="inspect-body">${empty('Проверок ещё нет', 'Результат появится здесь после сохранённого запуска этой области.')}</div>`;
  if (!run) return `<div class="inspect-body">${state.runErrors[runIdFrom(operation)]
    ? empty('Запуск недоступен', state.runErrors[runIdFrom(operation)])
    : empty('Читаем сохранённый запуск', `ID: ${runIdFrom(operation)}`)}</div>`;
  const axes = operation.result || {};
  const deliveryOperation = sortedOperations(state.operations).find(o => runIdFrom(o) === run.run_id && o.result?.delivery) || operation;
  const publicationOperation = sortedOperations(state.operations).find(o =>
    runIdFrom(o) === run.run_id && ['PUBLISH', 'DELIVER_VERIFY_PUBLISH', 'VERIFY'].includes(o.action) && o.result?.publication) || operation;
  const tab = state.tab;
  const tabs = [['summary', 'Сводка'], ['console', 'Консоль'], ['tests', 'Тесты'], ['artifacts', 'Файлы']];
  const resultNote = operation.status === 'COMPLETED' && run.execution?.outcome === 'FAILED'
    ? notice('Проверка кода проекта не прошла. Runner обслужил операцию; состояние публикации и доставки показано отдельно.')
    : operation.status === 'FAULT' ? notice('Сбой runner или инфраструктуры. Итог кода и сохранённые файлы показаны отдельно.') : '';
  let content = '';
  if (tab === 'summary') {
    content = `<div class="summary-grid"><div>${badge(operation.status)}<div class="label">Операция</div></div><div>${badge(run.execution?.outcome)}<div class="label">Исполнение проекта</div></div><div>${deliveryBadge(deliveryOperation.result?.delivery?.status || deliveryOperation.delivery_status || run.delivery?.status)}<div class="label">Доставка</div></div></div>
      ${row('Запуск', run.run_id)}${row('Проверенный commit', run.snapshot?.commit)}${row('Дерево снимка', run.snapshot?.tree)}${row('Ветка сейчас', workspace.branch)}${row('Публикация', label(publicationOperation.result?.publication?.status || publicationOperation.publication_status || 'NOT_REQUESTED'))}
      ${row('Продолжительность', typeof run.execution?.duration_seconds === 'number' ? `${run.execution.duration_seconds.toFixed(2)} с` : null)}${githubPrLink(publicationOperation.result?.publication)}
      ${resultNote}${notice(snapshotState(workspace, run, operation.result))}`;
  } else if (tab === 'console') {
    const outputs = state.outputs[run.run_id] || [];
    const logs = outputs.filter(o => ['console', 'runner_log', 'application_log'].includes(o.kind));
    const structured = logs.filter(o => o.kind === 'application_log' && o.format === 'application-log/v1');
    content = logs.length ? `<p class="subtitle">Файлы ограничены размером при просмотре. Выберите зарегистрированный поток; порядок stdout и stderr не объединяется искусственно.</p>${logs.map(o => outputButton(state, run.run_id, o)).join(' ')}` : empty('Консольных файлов нет', 'Зарегистрированных потоков у этого запуска нет.');
    if (structured.length) {
      content += `<div class="evidence-detail"><button type="button" class="btn small" data-action="log-errors-only" aria-pressed="${state.logErrorsOnly}">${state.logErrorsOnly ? 'Скрыть ERROR/FATAL' : 'Показать ERROR/FATAL'}</button><p class="subtitle">Фильтр применяется только к распознанным application-log/v1; исходные файлы доступны выше.</p>`;
      if (state.logErrorsOnly) content += structured.map(o => {
        const key = `${run.run_id}/${o.output_id}`;
        const inspection = state.inspections[key];
        if (retentionOutput(state, run.run_id, o.output_id)?.state === 'EXPIRED')
          return notice(`Журнал ${o.output_id} удалён; фильтр недоступен.`);
        if (state.inspectionErrors[key]) return notice(`Журнал ${o.output_id}: ${state.inspectionErrors[key]}`);
        if (!inspection) return notice(`Журнал ${o.output_id}: читаем сохранённые события…`);
        if (inspection.status !== 'PARSED') return notice(`Журнал ${o.output_id}: фильтр недоступен (${inspection.status}). Откройте исходный файл.`);
        const events = inspection.error_events || [];
        return `<h3>${esc(o.output_id)}</h3>${inspection.summary?.invalid_lines ? notice(`Некорректных строк: ${inspection.summary.invalid_lines}; они не включены в фильтр.`) : ''}${events.length ? events.map(event => `<div class="structured-event"><strong>${esc(event.level)} · ${esc(event.service)} · ${esc(event.event)}</strong><small>${esc(event.timestamp)}</small><p>${esc(event.message)}${event.message_truncated ? ' …' : ''}</p></div>`).join('') : empty('ERROR/FATAL не зарегистрированы', 'Только в распознанных строках этого журнала.')}${inspection.omitted_error_events ? notice(`Осталось ${inspection.omitted_error_events} событий; откройте исходный файл.`) : ''}`;
      }).join('');
      content += '</div>';
    }
  } else if (tab === 'tests') {
    const outputs = (state.outputs[run.run_id] || []).filter(o => o.kind === 'test_result');
    content = outputs.length ? `<p class="subtitle">Итоги показаны только для распознанного полного JUnit. Неполные и иные форматы остаются исходными файлами.</p>${outputs.map(o => {
      const key = `${run.run_id}/${o.output_id}`;
      const inspection = state.inspections[key];
      const raw = outputButton(state, run.run_id, o);
      if (retentionOutput(state, run.run_id, o.output_id)?.state === 'EXPIRED')
        return `<div class="evidence-detail">${raw}${notice('Исходный JUnit удалён; подробности недоступны.')}</div>`;
      if (state.inspectionErrors[key]) return `<div class="evidence-detail">${raw}${notice(state.inspectionErrors[key])}</div>`;
      if (!inspection) return `<div class="evidence-detail">${raw}${notice('Читаем сохранённый JUnit…')}</div>`;
      if (inspection.status !== 'PARSED') return `<div class="evidence-detail">${raw}${notice(`Число тестов неизвестно: ${inspection.status}. Откройте исходный файл.`)}</div>`;
      const summary = inspection.summary || {};
      const cases = inspection.failed_cases || [];
      return `<div class="evidence-detail">${raw}<p class="subtitle">Из элементов testcase: ${Number(summary.tests).toLocaleString('ru-RU')} всего · ${Number(summary.passed).toLocaleString('ru-RU')} прошло · ${Number(summary.failed).toLocaleString('ru-RU')} ошибок проверки · ${Number(summary.errors).toLocaleString('ru-RU')} ошибок запуска · ${Number(summary.skipped).toLocaleString('ru-RU')} пропущено.</p>${cases.map(item => `<details class="test-case"><summary>${esc(item.outcome)} · ${esc(item.id)}</summary>${item.message ? `<p>${esc(item.message)}</p>` : ''}<pre class="console">${esc(item.trace || 'Текст причины не записан в JUnit.')}</pre></details>`).join('')}${inspection.omitted_failed_cases ? notice(`Показаны первые ${cases.length} случаев; ещё ${inspection.omitted_failed_cases} доступны в исходном JUnit.`) : ''}</div>`;
    }).join('')}` : empty('Результаты тестов не зарегистрированы');
  } else content = outputList(state, run);
  return `<div class="inspect-body"><div class="runheadline"><div><strong>Последний сохранённый запуск этой области</strong><p>Операция ${esc(short(operation.request_id))} · ${esc(timeText(run.created_at))}</p></div>${badge(run.execution?.outcome)}</div></div>
    <div class="output-tabs" role="tablist" aria-label="Разделы результата">${tabs.map(([id, caption]) => `<button type="button" class="output-tab ${tab === id ? 'on' : ''}" role="tab" id="tab-${id}" aria-controls="run-panel" aria-selected="${tab === id}" data-action="tab" data-value="${id}">${caption}</button>`).join('')}</div>
    <div class="output-content" role="tabpanel" id="run-panel" aria-labelledby="tab-${tab}"><div class="retention-short"><span>Хранение: ${esc(retentionSummary(state, run.run_id))}</span>${button('retention', 'Хранение и очистка').replace('data-action="retention"', `data-action="retention" data-run="${esc(run.run_id)}"`)}</div>${content}</div>`;
}

function workView(state) {
  const project = currentProject(state);
  if (!project) return `<div class="content">${connectionStrip(state)}${empty('Нет выбранного проекта', 'Создайте проект или обновите список.')}</div>`;
  const workspaces = state.workspaces[project.project_id] || [];
  const workspace = currentWorkspace(state);
  const createWhy = eligibility(state, 'CREATE_WORKSPACE', project.project_id);
  const verifyWhy = workspace ? eligibility(state, 'VERIFY', project.project_id, workspace.workspace_id) : 'Выберите рабочую область.';
  const fetchWhy = eligibility(state, 'FETCH', project.project_id);
  const exportWhy = workspace ? eligibility(state, 'EXPORT_CONTEXT', project.project_id, workspace.workspace_id) : 'Выберите рабочую область.';
  return `<div class="content"><div class="projecthead"><div><div class="eyebrow">Проект · ${esc(project.project_id)}</div><h1 class="pagetitle">${esc(project.display_name || project.project_id)}</h1><p class="subtitle">${esc(label(project.state))} · Репозиторий ${esc(project.metadata?.remote?.owner && project.metadata?.remote?.name ? `${project.metadata.remote.owner}/${project.metadata.remote.name}` : 'не указан в проекции')}</p></div>
    <div class="actions">${button('code-menu', 'VS Code ▾', workspaces.length ? null : 'У проекта нет рабочих областей.')}${button('fetch', 'Git fetch', fetchWhy)}${button('new-project', 'Новый проект', eligibility(state, 'CREATE_PROJECT'), 'primary')}${reason(fetchWhy)}</div></div>
    ${connectionStrip(state)}
    <div class="workspace-layout"><section class="workspaces-side" aria-label="Рабочие области"><div class="sectiontitle"><h2>Рабочие области</h2><span class="sub">${workspaces.length} зарегистрировано</span></div><div class="workspaces" data-testid="workspace-list">${state.workspaceErrors[project.project_id] ? empty('Области недоступны', state.workspaceErrors[project.project_id]) : workspaces.map(w => workspaceCard(state, w)).join('') || empty('Областей пока нет')}</div>
      <div>${button('new-workspace', '＋ Новая рабочая область', createWhy, 'addws')}${reason(createWhy)}</div><p class="side-note">У каждой области собственная ветка, история и источник команд. Смена ветки в одной области не меняет соседние.</p></section>
    <section class="inspector" aria-label="Выбранная область">${workspace ? `<div class="inspect-head"><div><div class="eyebrow">Рабочая область · ${esc(workspace.workspace_id)}</div><h2>${esc(workspace.display_name || workspace.workspace_id)}</h2><div class="minor">${esc(workspace.branch || 'Ветка неизвестна')} · ${esc(sourceHead(workspace) || 'HEAD неизвестен')}</div></div><div class="orb" aria-hidden="true">◇</div></div>
      <div class="workspace-toolbar">${button('verify', 'Проверить', verifyWhy, 'primary')}${button('operation-modal', 'Новая операция', workspace ? null : 'Выберите рабочую область.')}${button('workspace-edit', 'Редактор области')}${button('workspace-drive', 'Файлы в Drive')}${button('workspace-editor', 'VS Code · эта область')}${button('check-git-remote', 'Проверить удалённую ветку')}${button('export-context', 'Выдать контекст', exportWhy)}${reason(exportWhy)}</div>
      <div class="workspace-meta"><div><div class="label">Ветка</div><div class="value mono">${text(workspace.branch)}</div></div><div><div class="label">Записанный HEAD</div><div class="value mono">${text(sourceHead(workspace))}</div></div><div><div class="label">Состояние файлов</div><div class="value">Не проверено</div></div></div>${runPanel(state, workspace)}` : empty('Выберите рабочую область')}</section></div>
    <section class="history"><div class="sectiontitle"><h2>Последние операции проекта</h2><button type="button" class="linkbtn" data-action="view" data-value="operations">Все операции →</button></div>${operationTable(projectOperations(state, project.project_id).slice(0, 5))}</section>
  </div>`;
}

function operationTable(operations) {
  if (!operations.length) return empty('Операций пока нет', 'Новый запрос запускается только по явному действию.');
  return `<div class="tablewrap" data-testid="operation-list"><table class="table"><thead><tr><th>Действие</th><th>Область</th><th>Операция</th><th>Исполнение</th><th>Публикация</th><th>Доставка</th><th>Время</th></tr></thead><tbody>${operations.map(o => `<tr><td><button type="button" class="linkbtn" data-action="operation" data-value="${esc(o.request_id)}">${esc(actionLabel(o.action))} →</button><small class="mono">${esc(short(o.request_id))}</small></td><td>${esc(o.project_id || '—')} / ${esc(o.workspace_id || '—')}</td><td>${badge(o.status)}</td><td>${badge(o.result?.execution?.outcome || 'NOT_RUN')}</td><td>${badge(o.result?.publication?.status || o.publication_status || 'NOT_REQUESTED')}</td><td>${deliveryBadge(o.result?.delivery?.status || o.delivery_status || 'NOT_ATTEMPTED')}</td><td>${esc(timeText(o.received_at))}</td></tr>`).join('')}</tbody></table></div>`;
}

function operationDetail(state) {
  const op = state.operations.find(o => o.request_id === state.operationId);
  if (!op) return '';
  const steps = op.steps || [];
  const effects = op.effects || [];
  const run = state.runs[runIdFrom(op)];
  const cancelWhy = op.status === 'RUNNING' ? eligibility(state, 'CANCEL_OPERATION', op.project_id, op.workspace_id) : 'Только выполняющуюся операцию можно остановить.';
  return `<section class="card" data-testid="operation-detail"><div class="fullhead"><div><div class="eyebrow">Операция ${esc(op.request_id)}</div><h2>${esc(actionLabel(op.action))}</h2></div>${badge(op.status)}</div>
    <div class="chip-row">${badge(op.result?.execution?.outcome || 'NOT_RUN')}${badge(op.result?.publication?.status || op.publication_status || 'NOT_REQUESTED')}${deliveryBadge(op.result?.delivery?.status || op.delivery_status || 'NOT_ATTEMPTED')}</div>
    ${row('Запрошено', timeText(op.received_at))}${row('Обновлено', timeText(op.updated_at))}${row('Проект / область', `${op.project_id || '—'} / ${op.workspace_id || '—'}`)}${row('Код', op.code)}${row('Намерение', op.command?.intent)}
    ${run ? row('Снимок запуска', run.snapshot?.commit) : ''}<p class="subtitle">Шаги ниже взяты из журнала операции. Их число зависит от фактического маршрута; пропущенные шаги не изображаются выполненными.</p>
    <ol class="stage-list">${steps.map(s => `<li><span class="n">•</span><span><strong>${esc(s.step_id)}</strong><small>${esc(s.last_event_type)} · ${esc(timeText(s.created_at))} → ${esc(timeText(s.updated_at))}</small></span></li>`).join('') || '<li>Записанных шагов пока нет.</li>'}</ol>
    <h3>Внешние эффекты</h3>${effects.length ? effects.map(e => `<div class="definition"><strong>${esc(e.kind)} · ${esc(e.step_id)}</strong><span>${badge(e.state)} <span class="mono">${esc(e.provider_id || 'ID ещё не подтверждён')}</span><small>${esc(timeText(e.updated_at))}</small></span></div>`).join('') : empty('Внешних эффектов нет')}
    <div class="actions">${button('cancel', 'Остановить', cancelWhy, 'danger')}${reason(cancelWhy)}${run ? button('run', 'Открыть запуск') : ''}${button('download-operation', 'Скачать JSON операции')}${githubPrLink(op.result?.publication)}</div>
  </section>`;
}

function operationsView(state) {
  const operations = sortedOperations(state.operations);
  return `<div class="content"><div class="fullhead"><div><div class="eyebrow">Конечные действия</div><h1 class="pagetitle">Операции</h1><p class="subtitle">Операция обслуживает запрос. Итог кода, публикация и доставка показаны отдельно. BUSY не ставится в очередь.</p></div>${button('refresh', 'Обновить')}</div>${connectionStrip(state)}${state.partialOperations ? notice('Показана только первая часть истории; локальный API ограничивает список. Обновите или откройте конкретный ID.') : ''}${operationTable(operations)}${operationDetail(state)}</div>`;
}

function resultsView(state) {
  const seen = new Set();
  const operations = sortedOperations(state.operations).filter(o => {
    const id = runIdFrom(o);
    if (!id || !['VERIFY', 'DELIVER_VERIFY_PUBLISH'].includes(o.action) || seen.has(id)) return false;
    seen.add(id); return true;
  });
  return `<div class="content"><div class="fullhead"><div><div class="eyebrow">Сохранённые снимки</div><h1 class="pagetitle">Результаты</h1><p class="subtitle">Каждый запуск относится к конкретному commit и run ID. Успех вчерашнего снимка не подтверждает текущие файлы.</p></div></div>${connectionStrip(state)}${state.partialRunEvidence ? notice('Список запусков ограничен первыми 100 записями; часть файлов не загружена.') : ''}<div data-testid="result-list">${operations.length ? operations.map(o => {
    const run = state.runs[runIdFrom(o)];
    const delivery = sortedOperations(state.operations).find(x => runIdFrom(x) === runIdFrom(o) && x.result?.delivery) || o;
    const publication = sortedOperations(state.operations).find(x =>
      runIdFrom(x) === runIdFrom(o) && ['PUBLISH', 'DELIVER_VERIFY_PUBLISH', 'VERIFY'].includes(x.action) && x.result?.publication) || o;
    const workspace = state.workspaces[o.project_id]?.find(w => w.workspace_id === o.workspace_id);
    return `<section class="card"><div class="fullhead"><div><div class="eyebrow">${esc(o.project_id)} / ${esc(o.workspace_id)}</div><h2>${esc(run?.run_id || runIdFrom(o))}</h2><p class="subtitle">${esc(timeText(run?.created_at || o.updated_at))}</p></div>${badge(run?.execution?.outcome || 'UNKNOWN')}</div>
      ${row('Проверенный commit', run?.snapshot?.commit)}${row('Операция', o.request_id)}${row('Публикация', label(publication.result?.publication?.status || publication.publication_status || 'NOT_REQUESTED'))}${row('Доставка', deliveryLabel(delivery.result?.delivery?.status || delivery.delivery_status || 'NOT_ATTEMPTED'))}
      ${notice(snapshotState(workspace, run, o.result))}${githubPrLink(publication.result?.publication)}<div class="retention-short"><span>Хранение: ${esc(retentionSummary(state, runIdFrom(o)))}</span><button type="button" class="btn small" data-action="retention" data-run="${esc(runIdFrom(o))}">Хранение и очистка</button></div><button type="button" class="btn" data-action="run" data-run="${esc(runIdFrom(o))}">Посмотреть файлы запуска</button></section>`;
  }).join('') : empty('Результатов пока нет', 'После сохранённого запуска здесь появится его снимок и файлы.')}</div></div>`;
}

function driveView(state) {
  const check = freshCheck(state.system?.connections?.drive);
  return `<div class="content"><div class="eyebrow">Установленное пространство</div><h1 class="pagetitle">Google Drive</h1><p class="subtitle">Команды и результаты привязаны к уже существующей установке. Эта страница не создаёт второе пространство.</p>${connectionStrip(state)}
    <section class="card"><h2>Свежесть проверки команды</h2><p>${esc(check.text)}. Работа локального сервиса сама по себе не доказывает доступ к Google.</p>${row('Корневая папка', 'Chat-Dev Runner')}${row('Режим', 'Подключение к существующей установке')}${button('check-drive', 'Проверить доступ к Drive')}${notice('Точные ID документов областей берутся только из регистрации runner; поиск по названию не используется.')}</section>
    <section class="card"><h2>Протокол для чата</h2><p>Один явный запрос создаёт одну конечную операцию. После записи команды чат завершает ход; новый шаг требует нового указания владельца. Полученный результат относится к сохранённому снимку и доступен в отдельном ходе.</p></section></div>`;
}

function systemView(state) {
  const system = state.system || {};
  const armed = !!system.armed;
  return `<div class="content"><div class="eyebrow">Локальное управление</div><h1 class="pagetitle">Система</h1><p class="subtitle">Состояние runner и подключений. Ошибки тестов проекта показаны рядом с запуском, а не как авария сервиса.</p>${connectionStrip(state)}
    <div class="twocol"><section class="card"><h2>Runner</h2>${row('Приём команд', armed ? 'Включён' : 'Выключен')}${row('Эпоха', system.epoch)}${row('Занято слотов', system.reserved_capacity == null ? null : `${system.reserved_capacity} / ${system.max_parallel}`)}${row('Очередь', 'Отсутствует')}${row('Локальные маршруты', (system.capabilities || []).map(actionLabel).join(', ') || 'Не подключены')}
      <div class="actions">${button(armed ? 'disarm' : 'arm', armed ? 'Остановить приём' : 'Включить приём', armed ? null : (system.capabilities || []).length ? null : 'Ни один производственный маршрут не подключён.', 'primary')}</div></section>
    <section class="card"><h2>Оформление</h2><div class="setting-row"><div><strong>Тема</strong><p>Светлая или тёмная мастерская.</p></div>${button('theme', state.theme === 'dark' ? 'Светлая' : 'Тёмная')}</div><div class="setting-row"><div><strong>Снизить движение</strong><p>Убирает анимацию, не меняя скорость работы.</p></div><button type="button" class="toggle ${state.motionOff ? 'on' : ''}" role="switch" aria-checked="${state.motionOff}" aria-label="Снизить движение" data-action="motion"></button></div></section></div>
    <section class="card"><h2>Отдельные подключения</h2><p>Проверка Drive, удалённой Git-ветки и GitHub API имеет собственную дату и результат. Проверка ветки использует ls-remote; она не выполняет fetch. Успешная проверка GitHub подтверждает учётную запись API; право создавать draft PR отдельно не проверяется.</p><div class="actions">${button('check-drive', 'Проверить Drive')}${button('check-github', 'Проверить GitHub API')}</div></section></div>`;
}

export function renderApp(state) {
  if (state.loading) return `<main class="loading-shell" data-testid="app"><div class="brandmark" aria-hidden="true"></div><h1>Runner</h1><p>Загружаем локальные данные…</p></main>`;
  if (!state.authenticated) return `<main class="login-shell" data-testid="login"><div class="login-card"><div class="brandmark" aria-hidden="true"></div><h1>Runner</h1><p>Локальная сессия владельца истекла или ещё не открыта.</p><form id="login-form"><label class="field"><span>Одноразовый локальный код</span><input id="bootstrap-input" data-testid="bootstrap-input" type="password" autocomplete="off" spellcheck="false" required></label><p class="subtitle">Код находится в локальном файле <code>session-bootstrap.json</code> вашего профиля runner. Он не сохраняется в браузере.</p><button class="btn primary" type="submit">Открыть сессию</button></form>${state.error ? `<p class="errorline" role="alert">${esc(state.error)}</p>` : ''}</div></main>`;
  const views = {work: workView, operations: operationsView, results: resultsView, drive: driveView, system: systemView};
  return `<div class="shell" data-testid="app">${sidebar(state)}<main class="main">${toolbar(state)}${(views[state.view] || workView)(state)}</main></div>`;
}

function selectField(name, title, choices, selected = '', hint = '', required = true) {
  const values = Array.isArray(choices) ? choices.filter(value => typeof value === 'string') : [];
  return `<label class="field"><span>${esc(title)}</span><select name="${esc(name)}" ${required ? 'required' : ''}>${values.length ? values.map(value => `<option value="${esc(value)}" ${value === selected ? 'selected' : ''}>${esc(value)}</option>`).join('') : '<option value="">Нет утверждённых вариантов</option>'}</select>${hint ? `<small>${esc(hint)}</small>` : ''}</label>`;
}
function field(name, title, value = '', hint = '', required = true) {
  return `<label class="field"><span>${esc(title)}</span><input name="${esc(name)}" value="${esc(value)}" ${required ? 'required' : ''}>${hint ? `<small>${esc(hint)}</small>` : ''}</label>`;
}

export function renderDialog(state) {
  const dialog = state.dialog;
  if (!dialog) return '';
  let title = '', body = '', footer = '';
  const project = currentProject(state), workspace = currentWorkspace(state);
  if (dialog.type === 'palette') {
    title = 'Перейти к…';
    body = `<label class="field"><span>Поиск</span><input id="palette-search" autocomplete="off" placeholder="Раздел или область"></label><div class="menu-list" id="palette-items">${[['work', 'Рабочие области'], ['operations', 'Операции'], ['results', 'Результаты'], ['drive', 'Google Drive'], ['system', 'Система']].map(([id, caption]) => `<button type="button" class="menu-item" data-action="palette-view" data-value="${id}">${caption}</button>`).join('')}${Object.entries(state.workspaces).flatMap(([pid, list]) => list.map(w => `<button type="button" class="menu-item" data-action="palette-workspace" data-project="${esc(pid)}" data-value="${esc(w.workspace_id)}">${esc(w.display_name || w.workspace_id)} <small>${esc(pid)} / ${esc(w.branch)}</small></button>`)).join('')}</div>`;
  } else if (dialog.type === 'new-project') {
    title = dialog.step === 2 ? 'Проверка нового проекта' : 'Новый проект';
    if (dialog.step === 2) {
      body = `<p>Будет создана одна операция CREATE_PROJECT. Репозиторий будет приватным. Локальный источник сначала получит краткоживущий импортный handle; произвольный путь не попадёт в команду.</p>${row('Название', dialog.draft.name)}${row('ID', dialog.draft.id)}${row('Источник', dialog.draft.sourceKind)}${row('Шаблон', dialog.draft.templateId)}${row('GitHub identity', dialog.draft.githubIdentity)}${dialog.draft.repository ? row('Репозиторий', dialog.draft.repository) : ''}${dialog.draft.localPath ? row('Локальный путь', dialog.draft.localPath) : ''}<p class="run-note">При отказе BUSY или конфликте запрос завершается. Повтор требует нового явного действия.</p>`;
      footer = `<button type="button" class="btn ghost" data-action="project-back">Назад</button><button type="button" class="btn primary" data-action="project-confirm">Создать проект</button>`;
    } else {
      const d = dialog.draft || {};
      body = `<form id="project-form">${field('name', 'Название', d.name)}${field('id', 'Постоянный ID', d.id, 'a–z, 0–9, дефис; до 48 символов')}
        <label class="field"><span>Источник</span><select name="sourceKind"><option value="new" ${d.sourceKind === 'new' ? 'selected' : ''}>Новый</option><option value="github" ${d.sourceKind === 'github' ? 'selected' : ''} ${state.system?.approved?.github_repositories?.length ? '' : 'disabled'}>Существующий GitHub</option><option value="local" ${d.sourceKind === 'local' ? 'selected' : ''}>Локальный Git</option></select></label>
        ${selectField('repository', 'Утверждённый GitHub owner/repository', state.system?.approved?.github_repositories, d.repository, 'Только для импорта из GitHub; список задаётся локальной политикой', false)}${field('localPath', 'Абсолютный путь к локальному Git', d.localPath, 'Только для локального импорта; путь остаётся на этой машине', false)}
        ${selectField('templateId', 'Разрешённый шаблон', state.system?.approved?.templates, d.templateId)}${selectField('githubIdentity', 'Разрешённая GitHub identity', state.system?.approved?.github_identities, d.githubIdentity)}
        <button type="submit" class="btn primary">Проверить план</button></form>`;
    }
  } else if (dialog.type === 'new-workspace') {
    title = `Новая область · ${project?.display_name || ''}`;
    body = `<p>Создаётся отдельный worktree от точного базового commit. Соседние области остаются на своих ветках.</p><form id="workspace-form">${field('name', 'Название')}${field('id', 'Постоянный ID')}${field('branch', 'Новая ветка')}${field('baseCommit', 'Базовый commit', sourceHead(workspace) || '', 'Полный 40- или 64-символьный SHA')}${selectField('profileId', 'Разрешённый профиль', state.system?.approved?.profiles, workspace?.metadata?.profile_id || '')}<button type="submit" class="btn primary">Создать область</button></form>`;
  } else if (dialog.type === 'operation') {
    title = 'Новая операция';
    const latest = workspace && latestRunOperation(state, state.projectId, workspace.workspace_id);
    const runId = latest && runIdFrom(latest);
    body = `<p>Область: <strong>${esc(project?.project_id || '')} / ${esc(workspace?.workspace_id || '')}</strong>. Один запрос немедленно принимается либо получает терминальный отказ.</p><form id="operation-form">
      <label class="field"><span>Действие</span><select name="action"><option value="VERIFY">Проверить текущий HEAD</option><option value="PUBLISH">Опубликовать сохранённый run ID</option><option value="PUBLISH_REPORT">Доставить отчёт повторно</option></select></label>
      ${selectField('profileId', 'Разрешённый профиль для VERIFY', state.system?.approved?.profiles, workspace?.metadata?.profile_id || '', 'Профиль владельца; не назначает новых полномочий', false)}${field('sourceRunId', 'Источник для публикации/доставки', runId || '', 'Полный сохранённый run ID', false)}
      <label class="field"><span>Правило публикации</span><select name="publication"><option value="draft_always">Черновик PR даже при падающем тесте</option><option value="on_pass">Только после прошедших тестов</option></select></label>
      ${field('intent', 'Цель операции', 'Проверить выбранную рабочую область')}<button class="btn primary" type="submit">Отправить один запрос</button></form>`;
  } else if (dialog.type === 'workspace-edit') {
    title = `Редактор области · ${workspace?.display_name || workspace?.workspace_id || ''}`;
    const source = dialog.source;
    const why = source ? retargetReason(state, source, state.projectId, state.workspaceId) : 'Состояние исходников ещё не проверено.';
    body = dialog.loading ? '<p>Проверяем точный HEAD, ветку и чистоту рабочей копии…</p>'
      : source ? `${row('Записанная ветка', source.recorded_branch)}${row('Наблюдаемая ветка', source.observed_branch)}${row('Записанный HEAD', source.recorded_head)}${row('Наблюдаемый HEAD', source.observed_head)}${row('Чистота', source.dirty === false ? 'Чисто' : source.dirty === true ? 'Есть изменения' : 'Неизвестно')}${row('Upstream', source.upstream)}${row('Впереди / позади', `${source.ahead ?? '—'} / ${source.behind ?? '—'}`)}${row('Разошлись', source.diverged === true ? 'Да' : source.diverged === false ? 'Нет' : 'Неизвестно')}${row('Проверено', timeText(source.checked_at))}
        ${notice(why || 'Область чистая и совпадает с записанной идентичностью. Итоговая проверка будет повторена сервером перед сменой ветки.')}
        <form id="retarget-form">${field('targetBranch', 'Целевая ветка', '', 'Новая или существующая ветка в этом проекте')}${field('targetCommit', 'Целевой commit', source.recorded_head, 'Полный 40- или 64-символьный SHA')}<button type="submit" class="btn primary" ${why ? `disabled title="${esc(why)}"` : ''}>Сменить ветку</button></form>`
      : empty('Исходники недоступны', 'Проверьте доступ к локальной рабочей области.');
    footer = '<button type="button" class="btn ghost" data-action="refresh-source">Проверить снова</button>';
  } else if (dialog.type === 'drive-docs') {
    title = `Drive · ${workspace?.display_name || workspace?.workspace_id || ''}`;
    const docs = dialog.docs;
    const safeId = value => typeof value === 'string' && /^[A-Za-z0-9_-]{10,200}$/.test(value);
    const folder = docs?.workspace_folder;
    const names = {command: 'COMMAND', changeset: 'CHANGESET', receipt: 'RECEIPT', latest_result: 'LATEST RESULT', project_state: 'PROJECT STATE', source_context: 'SOURCE CONTEXT', project_command: 'PROJECT COMMAND', project_receipt: 'PROJECT RECEIPT'};
    body = dialog.loading ? '<p>Проверяем точную регистрацию документов…</p>' : docs
      ? `<p>Ссылки построены из зарегистрированных ID этой области. Поиск по названию не используется.</p><div class="menu-list">${safeId(folder?.id) ? `<a class="menu-item" href="https://drive.google.com/drive/folders/${esc(folder.id)}" target="_blank" rel="noopener noreferrer">Папка области <small>${esc(folder.id)}</small></a>` : ''}${Object.entries(docs.documents || {}).map(([key, entry]) => safeId(entry?.id) ? `<a class="menu-item" href="https://docs.google.com/document/d/${esc(entry.id)}/edit" target="_blank" rel="noopener noreferrer">${esc(names[key] || key)} <small>${esc(entry.id)}</small></a>` : '').join('')}</div>${row('Регистрация проверена', timeText(docs.checked_at))}`
      : empty('Документы недоступны', 'Откройте их после успешной проверки точной регистрации.');
  } else if (dialog.type === 'export-context') {
    title = `Контекст · ${workspace?.display_name || workspace?.workspace_id || ''}`;
    body = `<p>Перечислите не более 100 относительных POSIX-путей внутри выбранной области. Домашние каталоги, .git, секреты и произвольные абсолютные пути запрещены.</p><form id="export-form"><label class="field"><span>Пути, по одному в строке</span><textarea name="paths" rows="7" placeholder="README.md&#10;src/main.py" required></textarea></label><label class="checkrow"><input type="checkbox" name="includeDiff"> Включить diff текущей области</label><button type="submit" class="btn primary">Выдать контекст</button></form>`;
  } else if (dialog.type === 'code') {
    title = 'Открыть в VS Code';
    body = `<p class="run-note">Откроется только точная зарегистрированная область. Путь и аргументы VS Code определяет локальный runner.</p><div class="menu-list">${(state.workspaces[state.projectId] || []).map(w => `<button type="button" class="menu-item" data-action="open-editor" data-value="${esc(w.workspace_id)}">${esc(w.display_name || w.workspace_id)} <small>${esc(w.workspace_id)} · ${esc(w.branch)}</small></button>`).join('')}</div>`;
  } else if (dialog.type === 'retention') {
    title = `Хранение запуска · ${dialog.runId}`;
    const status = state.retention?.[dialog.runId];
    if (dialog.loading) body = '<p>Проверяем закрепление и фактическое состояние выводов…</p>';
    else if (!status) body = empty('Данные хранения недоступны', 'Обновите статус после подключения локального хранилища.');
    else {
      const expired = status.outputs.some(item => item.state === 'EXPIRED');
      const selected = status.outputs.find(item => item.output_id === dialog.confirmOutputId);
      body = `<p>Запуск ${esc(dialog.runId)} · ${status.pinned ? 'закреплён владельцем' : 'не закреплён'}. Исходные файлы хранятся локально; запись об удалении остаётся после очистки.</p>
        <div class="actions">${button('pin-run', status.pinned ? 'Снять закрепление' : 'Закрепить запуск', dialog.busy ? 'Действие выполняется.' : !status.pinned && expired ? 'Уже удалённые выводы нельзя закрепить задним числом.' : null)}</div>
        <p class="run-note">Очистка одного файла проверяет 14-дневный срок, закрепление и ссылки на незавершённые доставки на сервере. До срока сервер откажет без удаления.</p>
        <div class="retention-list">${status.outputs.map(item => `<div class="retention-item"><div><strong>${esc(item.output_id)}</strong><small>${Number(item.size_bytes || 0).toLocaleString('ru-RU')} байт · SHA-256 ${esc(item.sha256)}</small><small>${item.state === 'EXPIRED' ? `Удалён ${esc(timeText(item.tombstone_at))} · ${esc(item.tombstone_reason || 'причина не указана')}` : item.state === 'AVAILABLE' ? 'Файл доступен' : 'Состояние неизвестно'}</small></div>${button('expire-output', 'Очистить', dialog.busy ? 'Действие выполняется.' : item.state === 'EXPIRED' ? 'Вывод уже удалён.' : item.state !== 'AVAILABLE' ? 'Состояние вывода неизвестно.' : status.pinned ? 'Сначала снимите закрепление запуска.' : null).replace('data-action="expire-output"', `data-action="expire-output" data-value="${esc(item.output_id)}"`)}</div>`).join('') || empty('Выводов нет')}
        </div>${selected && selected.state === 'AVAILABLE' ? `<div class="retention-confirm" role="group" aria-label="Подтверждение очистки"><strong>Очистить конкретный вывод ${esc(selected.output_id)}?</strong><p>Запуск ${esc(dialog.runId)} · SHA-256 ${esc(selected.sha256)}. Это удалит локальные байты после проверки срока и зависимостей, запись останется.</p><div class="actions"><button type="button" class="btn danger" data-action="expire-output-confirm" ${dialog.busy ? 'disabled title="Действие выполняется"' : ''}>Да, очистить этот вывод</button><button type="button" class="btn ghost" data-action="expire-output-cancel" ${dialog.busy ? 'disabled title="Действие выполняется"' : ''}>Отмена</button></div></div>` : ''}`;
    }
    footer = button('refresh-retention', 'Проверить статус снова', dialog.busy ? 'Действие выполняется.' : null, 'ghost');
  } else if (dialog.type === 'output') {
    title = `Вывод · ${dialog.outputId}`;
    const output = (state.outputs[dialog.runId] || []).find(o => o.output_id === dialog.outputId);
    body = output ? `${row('Тип', output.kind)}${row('Формат', output.format)}${row('Размер', `${output.size_bytes} байт`)}${row('SHA-256', output.sha256)}${row('Полнота', output.complete ? 'Полный' : `Неполный: ${output.truncated_reason || 'причина неизвестна'}`)}${row('Публикация', output.publication)}
      <p class="subtitle">Текст остаётся инертным. Изображение показывается только после проверки зарегистрированного растрового формата и размеров.</p>${state.preview?.url ? `<img class="preview-image" data-testid="image-preview" src="${esc(state.preview.url)}" alt="Сохранённое изображение ${esc(output.output_id)}"><p class="subtitle">${esc(state.preview.width)} × ${esc(state.preview.height)} пикселей · локальный проверенный вывод</p>` : `<pre class="console" data-testid="output-preview" id="output-preview" tabindex="0">${state.preview?.loading ? 'Читаем файл…' : state.preview?.error ? esc(state.preview.error) : state.preview && typeof state.preview.text === 'string' ? esc(state.preview.text || 'Файл пустой.') : 'Предпросмотр не выбран или недоступен.'}</pre>`}` : empty('Файл не найден');
    const expired = retentionOutput(state, dialog.runId, dialog.outputId)?.state === 'EXPIRED';
    footer = `<button type="button" class="btn ghost" data-action="retention-from-output">Хранение</button><button type="button" class="btn ghost" data-action="copy-output" ${state.preview?.text && !expired ? '' : 'disabled title="Нет доступного текста"'}>Копировать текст</button><button type="button" class="btn primary" data-action="download-output" ${output && !expired ? '' : 'disabled title="Вывод уже удалён"'}>Скачать файл</button>`;
  }
  return `<div class="overlay" data-action="backdrop"><section class="modal ${dialog.type === 'output' ? 'wide' : ''}" role="dialog" aria-modal="true" aria-labelledby="dialog-title" data-testid="dialog"><div class="modalhead"><div><h2 id="dialog-title">${esc(title)}</h2><p>${dialog.type === 'palette' ? 'Ctrl K · Escape для закрытия' : 'Локальная мастерская'}</p></div><button type="button" class="iconbtn" data-action="close-dialog" aria-label="Закрыть">×</button></div><div class="modalbody">${dialog.error ? `<p class="errorline" role="alert" tabindex="-1">${esc(dialog.error)}</p>` : ''}${body}</div>${footer ? `<div class="modalfoot">${footer}</div>` : ''}</section></div>`;
}
