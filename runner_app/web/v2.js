import {isUuid, timeText} from './model.js';

const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[char]);
const STATES = {
  ACK_PENDING: ['Подтверждение приёма', 'warn'], ACK_UNKNOWN: ['Приём не подтверждён', 'warn'],
  ACK_FAILED: ['Подтверждение не записано', 'warn'], QUEUED: ['В очереди', 'blue'],
  STARTING: ['Запускается', 'blue'], RUNNING: ['Выполняется', 'blue'],
  CONTROL: ['Управляющий запрос', 'blue'], CONTROL_RUNNING: ['Обрабатывается', 'blue'],
  COMPLETED: ['Обслужена', 'ok'], FAILED: ['Не пройдено', 'warn'], PASSED: ['Пройдено', 'ok'],
  REJECTED: ['Отклонён', 'warn'], BLOCKED: ['Остановлен условием', 'warn'], FAULT: ['Сбой runner', 'bad'],
  CANCELLED: ['Отменён', 'warn'], INTERRUPTED: ['Прерван', 'warn'],
  SUSPENDED_RESTART: ['Приостановлен после перезапуска', 'warn'],
  TIMED_OUT: ['Время вышло', 'warn'], NOT_RUN: ['Не запускалось', ''],
  INSUFFICIENT: ['Недостаточно данных', 'warn'], NOT_APPLICABLE: ['Не требуется', ''],
  CONFIRMED: ['Подтверждена', 'ok'], UNCERTAIN: ['Не подтверждена', 'warn'],
  NOT_NEEDED: ['Не требовалась', ''], UNKNOWN: ['Неизвестно', 'warn'],
  DELIVERED: ['Доставлено', 'ok'], LOCAL_ONLY: ['Только локально', ''],
  PARTIAL: ['Частично', 'warn'], NOT_REQUESTED: ['Не запрашивалась', ''],
  MISSING: ['Отсутствует', 'warn'], EXPIRED: ['Удалено', 'warn'],
  PUBLISHED: ['Опубликовано', 'ok'],
};
const ACTIONS = {RUN_TASK: 'Запуск задачи', SHELL: 'Явный shell', CANCEL: 'Отмена',
  STATUS: 'Запрос состояния', REDELIVER: 'Повторная доставка'};
const badge = value => {
  const [caption, tone] = STATES[value] || [value || 'Нет данных', ''];
  return `<span class="badge ${esc(tone)}" title="${esc(value || '')}">${esc(caption)}</span>`;
};
const row = (name, value) => `<div class="definition"><strong>${esc(name)}</strong><span>${esc(value ?? 'Нет данных')}</span></div>`;
const locator = (name, value) => `<div class="definition"><strong>${esc(name)}</strong><code>${esc(value || 'Не указан')}</code></div>`;
const note = value => `<p class="run-note">${esc(value)}</p>`;
const sequence = value => Number.isSafeInteger(value) && value > 0 ? String(value) : 'Не назначен';
const resultOf = request => request?.result?.manifest?.protocol === 'cdr-result/2' ? request.result.manifest : null;

export const hasV2 = state => state.v2?.available === true;
export function v2Configured(state) {
  const status = state.v2?.status;
  if (typeof status?.configured === 'boolean') return status.configured;
  return Object.values(status?.channels || {}).some(value =>
    typeof value === 'string' && !['GOOGLE_NOT_CONFIGURED', 'GOOGLE_UNAVAILABLE'].includes(value));
}
export function navigation(state) {
  const items = [['work', 'Рабочие области'], ['operations', 'Операции'], ['results', 'Результаты']];
  if (hasV2(state)) items.push(['v2', 'Задачи v2']);
  return [...items, ['drive', 'Google Drive'], ['system', 'Система']];
}

function requestTable(requests, selectedId) {
  if (!requests.length) return note('Запросов v2 пока нет. Показаны только записи этой установки.');
  return `<div class="tablewrap v2-table" data-testid="v2-request-list"><table class="table"><thead><tr><th>Запрос / проект</th><th>Состояние</th><th>FIFO</th><th>Обновлено</th></tr></thead><tbody>${requests.map(request => `<tr ${request.request_id === selectedId ? 'class="v2-selected"' : ''}><td>${isUuid(request.request_id) ? `<button type="button" class="linkbtn" data-action="v2-request" data-value="${esc(request.request_id)}" aria-pressed="${request.request_id === selectedId}">${esc(ACTIONS[request.action] || request.action || 'Неизвестное действие')} →</button>` : esc(request.action || 'Неизвестное действие')}<small>${esc(request.project_id || 'Нет проекта')}</small><small class="mono">${esc(request.request_id)}</small></td><td>${badge(request.status)}${request.code ? `<small class="mono">${esc(request.code)}</small>` : ''}</td><td>${esc(sequence(request.sequence))}</td><td>${esc(timeText(request.updated_at || request.created_at))}</td></tr>`).join('')}</tbody></table></div>`;
}

function artifacts(manifest) {
  const items = Array.isArray(manifest.artifacts) ? manifest.artifacts : [];
  if (!items.length) return note('Файлы в этом результате не зарегистрированы.');
  return `<div class="v2-artifacts">${items.map(item => `<article class="evidence-detail"><h3>${esc(item.id || 'Файл без ID')}</h3><div class="chip-row">${badge(item.availability)}<span class="subtitle">${esc(item.kind)} · ${esc(item.format)}</span></div>${locator('Локальный путь', item.local_path)}${row('Размер', Number.isSafeInteger(item.bytes) && item.bytes >= 0 ? `${item.bytes} байт` : null)}${locator('SHA-256', item.sha256)}${row('Полнота копии', item.complete === true ? 'Полная' : item.complete === false ? 'Неполная' : null)}${item.remote_path ? locator('Путь в пакете', item.remote_path) : ''}${item.reason ? row('Причина / код', item.reason) : ''}</article>`).join('')}</div>`;
}

function requestDetail(request, detailError) {
  if (!request) return `<section class="card" data-testid="v2-request-detail"><h2>Результат запроса</h2>${note('Выберите запрос, чтобы увидеть его подтверждённые факты и пути к файлам.')}</section>`;
  const manifest = resultOf(request);
  const delivery = request.result?.delivery;
  const execution = manifest?.execution;
  const axes = [['Операция', request.status], ['Исполнение', execution?.outcome],
    ['Критерий качества', execution?.quality_gate], ['Очистка', execution?.cleanup],
    ['Доставка сейчас', delivery?.status]];
  const control = !manifest && request.result?.target_request_id ? request.result : null;
  return `<section class="card" data-testid="v2-request-detail"><div class="eyebrow">${esc(request.project_id)} · ${esc(ACTIONS[request.action] || request.action)}</div><h2 class="mono">${esc(request.request_id)}</h2>${detailError ? `<p class="run-note warn" role="status">Детали не обновлены: ${esc(detailError)}</p>` : ''}
    <div class="v2-axes">${axes.map(([name, value]) => `<div class="definition" data-axis="${esc(name)}"><strong>${esc(name)}</strong><span>${badge(value)}</span></div>`).join('')}</div>
    ${row('Порядок FIFO', sequence(request.sequence))}${request.code ? row('Код запроса', request.code) : ''}${request.session_id ? locator('Сессия приёма', request.session_id) : ''}
    ${control ? `<h3>Целевой запрос</h3>${locator('ID', control.target_request_id)}${row('Состояние', control.status)}${row('Код', control.code)}${row('Порядок FIFO', sequence(control.sequence))}` : ''}
    ${manifest ? `<h3>Проверенный источник</h3>${row('Уровень результата', manifest.evidence_level === 'OBSERVED_RUN' ? 'Наблюдавшийся запуск' : manifest.evidence_level === 'EXAMPLE' ? 'Пример · не подтверждает запуск' : null)}${row('Ветка', manifest.source?.branch)}${locator('Commit', manifest.source?.commit)}${locator('Снимок workload', manifest.source?.workload_sha256)}${row('Изменения исходников', manifest.source?.dirty === true ? 'Были в снимке' : manifest.source?.dirty === false ? 'Не записаны' : null)}${row('Режим / ОС', [manifest.environment?.mode, manifest.environment?.execution_os, manifest.environment?.execution_arch].filter(Boolean).join(' · ') || null)}${row('Хост', [manifest.environment?.host_os, manifest.environment?.host_arch].filter(Boolean).join(' · ') || null)}${row('Профиль', manifest.environment?.profile_id)}${row('Код выхода', execution?.exit_code)}
      <h3>Пакет и доставка</h3>${locator('Локальный пакет', request.result?.bundle_path || delivery?.local_locator)}${row('Наличие пакета', delivery?.local_available === true ? 'Подтверждено runner' : delivery?.local_available === false ? 'Недоступен' : null)}${locator('SHA-256 пакета', request.result?.bundle_sha256)}${delivery?.code ? row('Код доставки', delivery.code) : ''}${delivery?.commit ? locator('Commit публикации', delivery.commit) : ''}${row('Ветка доставки', request.result?.branch)}
      ${note('Пути показаны как локальные адреса этой машины. Наличие пути в записи не заменяет проверку файла; удалённые ссылки не вычисляются из имени файла.')}
      <h3>Файлы · сохранённые сведения</h3>${artifacts(manifest)}${Array.isArray(manifest.warnings) && manifest.warnings.length ? `<details class="test-case"><summary>Замечания результата · ${manifest.warnings.length}</summary><ul>${manifest.warnings.map(value => `<li>${esc(value)}</li>`).join('')}</ul></details>` : ''}`
      : control ? '' : note('Сохранённого результата cdr-result/2 пока нет. Состояние приёма само по себе не подтверждает запуск, качество или доставку.')}
  </section>`;
}

export function renderV2View(state) {
  const v2 = state.v2 || {};
  const status = v2.status || {};
  const requests = Array.isArray(v2.requests) ? v2.requests : [];
  const selected = (v2.detail?.request_id === v2.selectedId ? v2.detail : null)
    || requests.find(request => request.request_id === v2.selectedId) || null;
  const capability = status.capability_status === 'IMPLEMENTED_NOT_QUALIFIED'
    ? 'Реализовано, квалификация не завершена' : status.capability_status || 'Неизвестно';
  const channels = Object.entries(status.channels || {});
  const profiles = Array.isArray(status.native_profiles) ? status.native_profiles : [];
  const projects = Array.isArray(status.projects) ? status.projects : [];
  return `<div class="content" data-testid="v2-view"><div class="fullhead"><div><div class="eyebrow">Живая сессия · v2</div><h1 class="pagetitle">Задачи и результаты</h1><p class="subtitle">Команды поступают через зарегистрированный GitHub-файл или Google Doc. Здесь — состояние запросов и сохранённые результаты.</p></div><button type="button" class="btn" data-action="refresh">Обновить</button></div>
    ${(v2.error || state.offline) ? `<p class="run-note warn" data-testid="v2-stale" role="status">Данные v2 могут быть устаревшими. ${esc(v2.error || 'Локальный сервис недоступен.')}</p>` : ''}
    <section class="card v2-runtime"><h2>Приём ${status.armed === true ? 'включён' : status.armed === false ? 'выключен' : 'неизвестен'}</h2><p>${esc(capability)}</p>${row('Runner', status.runner_id)}${locator('Сессия', status.session_id)}${row('Предел очереди', Number.isSafeInteger(status.queue_limit) ? status.queue_limit : null)}${row('Проекты v2', projects.join(', ') || null)}${row('Настроенные native-профили', profiles.join(', ') || null)}${row('Локальный статус прочитан', v2.checkedAt ? timeText(v2.checkedAt) : null)}
      <details class="v2-channels"><summary>Каналы · ${channels.length}</summary>${channels.length ? channels.map(([id, value]) => row(id, value)).join('') : note('Зарегистрированных каналов в ответе нет.')}</details></section>
    <div class="v2-layout"><section class="card"><h2>Запросы и очередь</h2><p>FIFO назначается после подтверждения приёма. Неопределённый ACK не означает запуск. После перезапуска задачи не возобновляются автоматически.</p>${requestTable(requests, v2.selectedId)}${requests.length >= 100 ? note('Показаны последние записи API. Отсутствие старого ID в этом списке не означает его удаление.') : ''}</section>${requestDetail(selected, v2.detailError)}</div></div>`;
}
