const ACTIONS = {
  CREATE_PROJECT: 'Создать проект', CREATE_WORKSPACE: 'Создать рабочую область',
  RETARGET_WORKSPACE: 'Сменить ветку', DELIVER_VERIFY_PUBLISH: 'Доставить · проверить · опубликовать',
  VERIFY: 'Проверить снимок', PUBLISH: 'Опубликовать проверенный снимок',
  FETCH: 'Обновить отслеживаемые ветки Git', SYNC_WORKSPACE: 'Синхронизировать область',
  EXPORT_CONTEXT: 'Собрать локальный контекст', ARCHIVE_WORKSPACE: 'Архивировать область',
  PUBLISH_REPORT: 'Повторно доставить отчёт', CANCEL_OPERATION: 'Остановить операцию',
  RECONCILE: 'Сверить побочные эффекты', RESUME_PROVISION: 'Завершить создание проекта',
};
const STATUS = {
  LOCAL_READY: ['Локально готов', 'ok'], READY: ['Готов', 'ok'], ACTIVE: ['Действует', 'ok'],
  ARCHIVED: ['В архиве', ''], PROVISIONING: ['Создаётся', 'blue'],
  RUNNING: ['Выполняется', 'blue'], COMPLETED: ['Обслужена', 'ok'],
  BLOCKED: ['Остановлена условием', 'warn'], FAULT: ['Сбой runner', 'bad'],
  INTERRUPTED: ['Прервана', 'warn'], CANCELLED: ['Отменена', 'warn'],
  REJECTED: ['Отклонена', 'warn'], BUSY: ['Занято — не принято', 'warn'],
  PASSED: ['Пройдены', 'ok'], FAILED: ['Не пройдены', 'warn'],
  TIMED_OUT: ['Время вышло', 'warn'], NOT_RUN: ['Не запускались', ''],
  UNKNOWN: ['Неизвестно', 'warn'], DELIVERED: ['Доставлено', 'ok'],
  PENDING: ['Ожидает доставки', 'warn'], PARTIAL: ['Частично', 'warn'],
  NOT_ATTEMPTED: ['Не выполнялось', ''], DRAFT_PUBLISHED: ['Черновик PR создан', 'ok'],
  NOT_REQUESTED: ['Не запрашивалось', ''], SKIPPED_POLICY: ['Пропущено политикой', ''],
  SKIPPED_FAILED_TESTS: ['Не опубликовано после тестов', 'warn'],
  NO_DIFF: ['Нет изменений', ''], CONFIRMED: ['Подтверждено', 'ok'],
  INTENDED: ['Намерение сохранено', 'warn'],
};
export const label = value => STATUS[value]?.[0] || (value ? String(value) : 'Нет данных');
export const tone = value => STATUS[value]?.[1] || '';
export const actionLabel = value => ACTIONS[value] || value || 'Неизвестное действие';
export const short = (value, size = 12) => typeof value === 'string' ? value.slice(0, size) : '—';
export const isSha = value => typeof value === 'string' && /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(value);
export const isUuid = value => typeof value === 'string' && /^[a-f0-9]{8}-[a-f0-9]{4}-[1-8][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(value);
export const isSlug = value => typeof value === 'string' && /^[a-z0-9][a-z0-9-]{0,47}$/.test(value);
export const timeText = value => {
  const date = typeof value === 'string' ? new Date(value) : null;
  return date && !Number.isNaN(date.valueOf()) ? date.toLocaleString('ru-RU') : 'Неизвестно';
};
export const sortedOperations = operations => [...operations].sort((a, b) =>
  Number(b.sequence || 0) - Number(a.sequence || 0));
export const projectOperations = (state, projectId) => sortedOperations(state.operations.filter(o => o.project_id === projectId));
export const workspaceOperations = (state, projectId, workspaceId) =>
  projectOperations(state, projectId).filter(o => o.workspace_id === workspaceId);
export const latestRunOperation = (state, projectId, workspaceId) =>
  workspaceOperations(state, projectId, workspaceId).find(o => ['VERIFY', 'DELIVER_VERIFY_PUBLISH'].includes(o.action) && isUuid(o.result?.run_id));
export const sourceHead = workspace => isSha(workspace?.metadata?.head) ? workspace.metadata.head : null;
export const snapshotState = (workspace, run, producerResult = null) => {
  if (!run) return 'Запусков для этой области пока нет.';
  const tested = run.snapshot?.commit;
  if (!isSha(tested)) return 'Снимок запуска не указан.';
  if (producerResult?.editor_matches_tested === false) {
    const editor = isSha(producerResult.editor_head) ? ` Редактор: ${short(producerResult.editor_head)}.` : '';
    return `Редактор не совпадает с проверенным снимком.${editor} Результат не подтверждает текущие файлы.`;
  }
  const head = isSha(producerResult?.editor_head) ? producerResult.editor_head : sourceHead(workspace);
  if (!head) return 'Текущий HEAD не подтверждён; актуальность снимка неизвестна.';
  if (tested !== head) return 'HEAD области изменился после снимка. Этот результат устарел.';
  if (producerResult?.editor_matches_tested === true)
    return 'Редактор совпадал с проверенным снимком при публикации. Текущее состояние файлов не проверено.';
  return 'Снимок совпадает с записанным HEAD. Чистота рабочей копии сейчас не проверена.';
};
export const runIdFrom = operation => {
  const value = operation?.result?.run_id || operation?.result?.source_run_id;
  return isUuid(value) ? value : null;
};

export function command(system, action, projectId, workspaceId, expectedHead, params, intent) {
  if (!isSlug(projectId) || (workspaceId !== null && !isSlug(workspaceId))) throw new Error('Неверная область команды.');
  if (!isUuid(system?.epoch) || !isSlug(system?.runner_id)) throw new Error('Текущая эпоха runner неизвестна. Обнови состояние.');
  const issued = new Date();
  return {
    schema_version: '1.0', request_id: crypto.randomUUID(), dispatch: 'READY',
    runner_id: system.runner_id, epoch: system.epoch,
    issued_at: issued.toISOString(), expires_at: new Date(issued.valueOf() + 300000).toISOString(),
    action, project_id: projectId, workspace_id: workspaceId,
    expected_head: expectedHead, params, intent,
  };
}

export function eligibility(state, action, projectId, workspaceId = null) {
  if (!state.system) return 'Состояние runner ещё не загружено.';
  if (!state.system.armed) return 'Runner не вооружён для новых команд.';
  if (!state.system.capabilities?.includes(action)) return `Маршрут ${action} не подключён локально.`;
  if (state.offline) return 'Локальный сервис недоступен; обновите состояние.';
  if (action === 'CREATE_PROJECT' && (!state.system.approved?.templates?.length || !state.system.approved?.github_identities?.length))
    return 'Нет утверждённого шаблона или GitHub identity в локальной политике.';
  if (['CREATE_WORKSPACE', 'VERIFY'].includes(action) && !state.system.approved?.profiles?.length)
    return 'Нет утверждённого профиля исполнения в локальной политике.';
  if (!isUuid(state.system.epoch)) return 'Нет подтверждённой эпохи runner.';
  if (workspaceId && !state.workspaces[projectId]?.some(w => w.workspace_id === workspaceId)) return 'Область не найдена.';
  return null;
}

export function freshCheck(value) {
  // A heartbeat or static configuration never proves a provider check.
  if (!value || typeof value !== 'object') return {text: 'Неизвестно', tone: ''};
  const at = value.last_checked_at || value.checked_at;
  const success = value.last_success_at;
  const status = value.status;
  if (!at || !['OK', 'ERROR'].includes(status)) return {text: 'Неизвестно', tone: ''};
  const age = Date.now() - Date.parse(at);
  const stale = !Number.isFinite(age) || age < -30000 || age > 300000;
  const lastSuccess = success ? ` · последний успех ${timeText(success)}` : '';
  if (stale) return {text: `Проверка устарела · ${timeText(at)}${lastSuccess}`, tone: 'warn'};
  if (status === 'ERROR') return {text: `Ошибка ${value.code || ''} · ${timeText(at)}${lastSuccess}`, tone: 'warn'};
  return {text: `Доступно · ${timeText(at)}`, tone: 'ok'};
}

export function retargetReason(state, source, projectId, workspaceId) {
  const route = eligibility(state, 'RETARGET_WORKSPACE', projectId, workspaceId);
  if (route) return route;
  if (!source) return 'Состояние исходников ещё не проверено.';
  if (!isSha(source.recorded_head) || !isSha(source.observed_head)
      || source.recorded_branch !== source.observed_branch
      || source.recorded_head !== source.observed_head
      || source.matches_recorded !== true) return 'Записанная и наблюдаемая Git-идентичность расходятся.';
  if (source.dirty !== false) return 'Рабочая копия содержит изменения или её чистота неизвестна.';
  if (source.diverged === true) return 'Удалённая и локальная ветки разошлись.';
  const age = Date.now() - Date.parse(source.checked_at);
  if (!Number.isFinite(age) || age < -30000 || age > 60000) return 'Проверка исходников устарела; проверьте снова.';
  if (state.partialOperations) return 'История операций неполна; нельзя подтвердить отсутствие занятой области.';
  if (state.operations.some(op => op.project_id === projectId && op.status === 'RUNNING'))
    return 'В этом проекте уже выполняется операция; новой очереди нет.';
  const workspace = state.workspaces[projectId]?.find(item => item.workspace_id === workspaceId);
  if (sourceHead(workspace) !== source.recorded_head || workspace?.branch !== source.recorded_branch)
    return 'Локальная проекция области устарела; обновите страницу.';
  return null;
}
