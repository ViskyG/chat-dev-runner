# Project enrollment / Регистрация проекта

## English

Use with [prepare-runner-project-v2](../skills/v2/prepare-runner-project-v2/SKILL.md)
for an authorized registration or repair. This procedure describes the current
[v2 configuration](V2_OPERATIONS.md#owner-configuration-and-service); it does not
register legacy v1 SourceRegistry entries or qualify task execution.

### 1. Resolve the requested route

Read the installed service/version, actual XDG paths, durable `runner_id`,
existing configuration and enrollment catalog. Derive the route from the request
and established workflow: local binding, GitHub mailbox, or Docs/handoff access
from chat. GitHub-only does not require Drive. For a folder intended to receive
chat handoffs, complete the target, Drive, mailbox and discovery steps below.
The registration request authorizes necessary scoped registration resources;
reuse it without another permission round. New credential grants, broader
repository access or execution capabilities require their own authorization.

Record the project ID and, when a local workspace is intended, its ID and exact
canonical source directory. Bind the requested folder itself, preserving branch,
dirty/untracked files and inclusion rules. A repository-only GitHub source needs
no clone or local binding. Do not silently substitute a managed copy for a
requested folder. Reconcile existing resources by ID before creating missing
ones; names alone are not proof.

### 2. Verify GitHub and runtime identities

When GitHub is requested, create or adopt the intended repository under the
existing visibility policy. Read its actual `id`, `node_id`, `owner`, `owner_id`
and `name`; use those pins in `projects[].github`. Verify the intended published
branch/commit, remote default branch and local upstream when applicable. Complete the authorized
new-repository branch setup without retargeting unrelated existing work.

For an intended local workspace, record `workspaces[workspace_id].path` in
owner-local `runtime-v2.json`. For a GitHub command channel, bind the selected
repository, existing branch and exact `.runner/commands/` path. Create the missing
command file there using the shared inert initialization below; validate and read
it through the installed `GitHubMailboxPort.read_current`. Use the current
blob/ref guard for writes, preserve an existing pending slot and unrelated files,
and reread conflicts or uncertain writes before retrying. A repository pin or
branch/path config alone does not provision a mailbox.
Runner credentials/owner-local GitHub access and the chat GitHub connector are
independent: read the exact repo through each required client. A connector 404
may mean missing selected-repository access; report that gap, preserving the
owner's selected-repositories policy. Never switch to “All repositories”.

#### Initialize a new command slot — GitHub or Docs

Initialize only a new command slot as schema-valid `cdr-mailbox/2`, actual runner
and project IDs, fresh UUID `slot_token`, `dispatch: DRAFT`, `receipt: null` and
an inert request. The established bootstrap request is `STATUS` with
`request_id: null` and a UUID placeholder `target_request_id` (not a real request).
Validate with the installed `validate_mailbox_shape`; keep this placeholder
**DRAFT forever**, replacing it only for later separately requested work.
Never seed READY, submit a sample task or overwrite an existing pending slot.

### 3. Provision the requested Docs route

Reuse the existing Drive root and installation index; do not bootstrap another
root, replace OAuth credentials/scopes, or repurpose protected v1 Docs. Create or
adopt a dedicated native command Doc for a Docs mailbox. For local handoffs,
also provision the scoped input folder and separate native result Doc. A Docs
task-only mailbox needs no local handoff target, input folder or handoff result Doc.
Read each actual tab ID; do not assume `t.0` or infer IDs from document titles.

Preflight with the **same OAuth application used by the installed Runner**.
For each required command/result Doc, require the exact native Doc identity, non-trashed
state, `capabilities.canEdit: true` and `isAppAuthorized: true`, then read the
exact tab. Connector-created Docs can be editable yet fail this app check with
`DOCS_FILE_IDENTITY_CHANGED`. Prefer creation through the existing Runner OAuth
provider, or verified explicit per-file app access; never bypass the validator.
For handoffs, read the input folder's type, parent and required access through that provider.
Independently confirm connector access to the resources needed by chat.

Initialize a new command tab using the shared inert procedure above.
When handoffs are requested, initialize the dedicated result tab empty or with
`{"protocol":"cdr-handoff-results/1","results":{}}`.

For local handoffs, bind `project.handoff_targets[target_id]` to its registered `workspace_id`, exact
`allowed_drive_parents` and paired `result_document_id`/`result_tab_id`. The local
inbox is `.runner/handoffs/`; the chat does not choose a host path. For either
Docs route, add the project's `provider: drive` mailbox with exact command `document_id`/`tab_id`.
Result Docs must be separate from every command Doc. Preserve all other targets,
mailboxes, profile grants and limits; registration does not add execution rights.

### 4. Activate and read back the installed configuration

Prepare a private config backup and rollback. Validate the candidate with the
installed `runner_app.config_v2.parse_v2_config`, and validate the private-file
layout with `load_v2_config` when applied (config 0600, directory 0700).
Before activation, preflight each requested channel through its installed
provider's read-only `GitHubMailboxPort.read_current` or
`DocsMailboxPort.read_current`, respectively. For Docs handoff, also inspect
the result tab format and access without publishing a fake receipt.

Before activation, read the current work and channel states. If a lifecycle
change would interrupt active/queued work or unacknowledged READY, retain the
prepared changes and defer activation until they are settled. Apply the config
using the installed service's verified reload procedure, or safely restart that
same service once if reload is unavailable. A full stopped-state backup is needed
only when the actual service/schema procedure requires it; config-only enrollment
does not change binaries/schema. Allow normal startup baselining, then read actual
runtime status: requested project/workspace/target bindings match, intake is armed
unless a real reconciliation gate blocks it, and each requested new channel has
a healthy idle observation.
Compare prior channels to their baseline; preserve healthy channels and report
pre-existing outages separately without expanding this task into their repair.
Check no workload was submitted by registration. On failure, retain partial-state evidence and
use the prepared rollback without replaying commands or blind repeated restarts.

### 5. Publish discovery and verify from chat

For a GitHub chat route, provide the actual repository, command branch and path
in its enrollment handoff, then read that exact file through the required chat
connector and compare it with the runtime binding. A GitHub-only route needs no
Docs catalog. Record any missing connector access separately from Runner access.

For a Docs task-only chat route, provide and read the exact command document/tab
through the chat connector and compare the runtime binding; no handoff catalog is required.

For the established Docs handoff workflow, read the existing **HANDOFF PROJECTS
— active enrollments** catalog through its registered ID and exact tab/revision.
It uses `cdr-handoff-catalog/1`, actual `runner_id`, and a `projects` object keyed
by `project_id`. Merge only this project's entry, preserving every other entry
and existing metadata, skill links and notes. Its observed shape contains
`project_id`, `command_document_id`, `command_tab_id`, and `targets[target_id]`
with `workspace_id`, `allowed_drive_parents`, `result_document_id`,
`result_tab_id`, `relative_inbox: ".runner/handoffs"`. Use actual bindings, never
example IDs. If this installation has no catalog, establish its intended
discovery record within the requested route instead of inventing a live ID.

Use revision-guarded Docs editing (`requiredRevisionId`); on conflict reread and
merge, and after an uncertain write reconcile by readback before retrying.
Preserve unrelated tabs. Create or update the project's ENROLLMENT tab with safe
bindings if that is the installation's established convention. No credentials or absolute private
host paths belong in the discovery record.

Independently read the catalog through the **chat connector**. Compare the exact
new entry to runtime bindings and prove all prior entries survived. Read command
and result tabs through that connector too. Local config/UI or SDK visibility
alone does not prove that a fresh chat can discover and use the registration.

### Completion evidence

Report the exact project/workspace, repository and branch, requested channel
and target, safe discovery link, installed channel readback and remaining gates.
Distinguish **runtime/repository registered**, **chat route registered**, and
**task execution qualified**. Mark registration complete only when every part
of the requested route is verified; otherwise name the missing leg and completed
parts. An idle DRAFT proves enrollment, not task execution, delivery, connector
guarded-write support or automatic Drive file-version metadata availability.
Runner 0.3.0.dev6 can resolve an omitted artifact version itself; use the
[handoff skill's version policy](../skills/usage/chatgpt/deliver-handoff-to-runner/SKILL.md#check-actual-transport-capabilities)
when qualifying delivery, rather than requiring the connector to expose it.

## Русский

Применять с [prepare-runner-project-v2](../skills/v2/prepare-runner-project-v2/SKILL.ru.md)
для разрешённой регистрации или её исправления. Процедура относится к текущей
[конфигурации v2](V2_OPERATIONS.md#owner-configuration-and-service); она не создаёт
записи legacy v1 SourceRegistry и не подтверждает выполнение задач.

### 1. Определить запрошенный маршрут

Прочитать фактические службу/версию, XDG-пути, постоянный `runner_id`, конфигурацию
и каталог регистраций. По запросу и текущему процессу выбрать локальную привязку,
GitHub mailbox либо доступ к Docs/handoff из чата. GitHub-only не требует Drive.
Для папки, принимающей пакеты из чата, завершить target, Drive, mailbox и каталог
по пунктам ниже. Запрос регистрации разрешает необходимые ресурсы в его пределах;
не переспрашивать это разрешение. Новые credential grants, расширение доступа к
репозиториям и прав исполнения требуют отдельного основания.

Зафиксировать project ID; если предусмотрена локальная область — также её ID и
точную каноническую исходную директорию. Привязать указанную папку, сохранив ветку,
dirty/untracked и правила включения. Для источника только из GitHub-репозитория
клон и локальная привязка не нужны. Не подменять запрошенную папку управляемой
копией. До создания недостающих ресурсов сверить существующие по ID; совпадения
имени недостаточно.

### 2. Проверить GitHub и runtime-привязки

Если запрошен GitHub, создать или подключить нужный репозиторий с действующей
политикой видимости. Прочитать реальные `id`, `node_id`, `owner`, `owner_id`,
`name` и закрепить их в `projects[].github`. Проверить опубликованные ветку/коммит,
удалённую default branch и локальный upstream, когда он применим. Завершить разрешённую настройку
ветки нового репозитория, не перенаправляя чужую существующую работу.

Для нужной локальной области записать `workspaces[workspace_id].path` в приватный
`runtime-v2.json`. Для GitHub-канала привязать выбранный репозиторий, существующую
ветку и точный путь `.runner/commands/`. Создать там недостающий command file
по общей неактивной инициализации ниже; проверить и прочитать его установленным
`GitHubMailboxPort.read_current`. Записывать с проверкой текущего blob/ref,
сохраняя ожидающий slot и чужие файлы; при конфликте или неопределённом исходе
перечитать до повтора. Одних repository pins или branch/path в конфигурации
недостаточно для создания mailbox. Доступ Runner или
локального GitHub-клиента и доступ GitHub-коннектора чата независимы: прочитать
точный репозиторий каждым необходимым клиентом. При 404 коннектора сообщить о
возможном отсутствии selected-repository access; сохранить выбор владельца,
не переключать приложение на «All repositories».

#### Новый command slot — GitHub или Docs

Только новый command slot заполнить валидным `cdr-mailbox/2`: реальные runner и
project IDs, новый UUID `slot_token`, `dispatch: DRAFT`, `receipt: null` и
неактивный запрос. Применяемый bootstrap — `STATUS` с `request_id: null` и UUID
заглушкой `target_request_id`, которая не является реальным запросом. Проверить
установленным `validate_mailbox_shape`; заглушка **всегда остаётся DRAFT**, до
замены для отдельно запрошенной работы. Не записывать READY, не запускать пробную
задачу и не затирать существующий ожидающий slot.

### 3. Подготовить запрошенный Docs-маршрут

Использовать существующие Drive root и installation index. Не создавать второй
root, не менять OAuth credentials/scopes и не переиспользовать защищённые v1
Docs. Для Docs mailbox создать или подключить отдельный нативный command Doc.
Для локальных handoff также подготовить выделенную входную папку и отдельный
нативный result Doc. Docs mailbox только для задач не требует локального handoff
target, входной папки или handoff result Doc. Прочитать реальные tab IDs; не предполагать `t.0` и не
выводить ID из названий.

Выполнить preflight **тем же OAuth-приложением, что использует установленный
Runner**. Для каждого нужного command/result Doc проверить точный ID, нативный тип, отсутствие
в корзине, `capabilities.canEdit: true`, `isAppAuthorized: true` и чтение нужной
вкладки. Созданный коннектором Doc может быть редактируемым, но давать Runner
`DOCS_FILE_IDENTITY_CHANGED`. Предпочесть создание существующим OAuth-провайдером
Runner либо проверенный явный доступ приложения к файлу; не обходить валидатор.
Для handoff тем же провайдером проверить тип, parent и нужные права входной папки. Независимо
проверить доступ коннектора к необходимым чату ресурсам.

Новую command tab инициализировать по общей неактивной процедуре выше.
Если запрошен handoff, выделенную result tab оставить пустой либо записать
`{"protocol":"cdr-handoff-results/1","results":{}}`.

Для локальных handoff связать `project.handoff_targets[target_id]` с зарегистрированным `workspace_id`,
точными `allowed_drive_parents` и парой `result_document_id`/`result_tab_id`.
Локальный inbox — `.runner/handoffs/`; чат не задаёт путь на машине. Для обоих
Docs-маршрутов добавить mailbox проекта с `provider: drive` и точными command `document_id`/`tab_id`.
Result Doc должен отличаться от всех command Docs. Сохранить остальные targets,
mailboxes, grants профилей и лимиты; регистрация не добавляет права исполнения.

### 4. Применить и прочитать установленную конфигурацию

Подготовить приватную копию конфигурации и откат. Проверить кандидат установленным
`runner_app.config_v2.parse_v2_config`; после применения проверить защищённые
файлы через `load_v2_config` (файл 0600, директория 0700). До включения каждого
запрошенного канала выполнить read-only `GitHubMailboxPort.read_current` либо
`DocsMailboxPort.read_current` соответствующим установленным провайдером.
Для Docs handoff также проверить формат и доступ result tab без фиктивной квитанции.

До активации прочитать состояние работы и каналов. Если смена состояния службы
прервёт активную/ожидающую работу или непринятый READY, сохранить подготовленные
изменения и отложить активацию до их завершения. Применить конфигурацию проверенным
reload-механизмом установленной службы; если его нет, один раз безопасно
перезапустить ту же службу. Полная stopped-state копия нужна только по требованиям
фактической процедуры службы/схемы; обычная регистрация не меняет бинарник/схему.
Дать завершиться штатному baseline при запуске, затем прочитать runtime status:
запрошенные привязки project/workspace/target совпадают, приём armed, если нет
настоящего reconciliation gate, каждый запрошенный новый канал даёт здоровое
idle-наблюдение. Сравнить прежние каналы с исходным
состоянием, сохранить здоровые, отдельно сообщить о ранее существовавших сбоях,
не расширяя задачу до их исправления. Проверить, что регистрация не отправляла задач. При
ошибке сохранить сведения о частичном состоянии и выполнить подготовленный откат
без повтора команд и слепых многократных рестартов.

### 5. Опубликовать обнаружение проекта и проверить из чата

Для GitHub-маршрута чата передать реальные репозиторий, command branch и path
в описании регистрации; прочитать этот точный файл нужным коннектором чата и
сверить с runtime-привязкой. GitHub-only не требует Docs-каталога. Отсутствие
доступа коннектора отражать отдельно от доступа Runner.

Для Docs-маршрута только для задач передать и прочитать точные command document/tab
коннектором чата, сверив runtime-привязку; handoff-каталог не требуется.

Для существующего Docs handoff процесса прочитать каталог **HANDOFF PROJECTS —
active enrollments** по зарегистрированным ID, вкладке и revision. Он использует
`cdr-handoff-catalog/1`, фактический `runner_id` и объект `projects` с ключами
`project_id`. Изменить только запись этого проекта, сохранив остальные записи,
metadata, ссылки на скиллы и notes. Наблюдаемый формат записи: `project_id`,
`command_document_id`, `command_tab_id` и `targets[target_id]` с `workspace_id`,
`allowed_drive_parents`, `result_document_id`, `result_tab_id`,
`relative_inbox: ".runner/handoffs"`. Использовать реальные привязки. Если у этой
установки каталога нет, оформить нужную запись обнаружения в рамках запрошенного
маршрута, не выдумывая живой ID.

Редактировать Docs с `requiredRevisionId`; при конфликте перечитать и объединить,
при неопределённом исходе сначала сверить фактическую запись. Сохранить чужие
вкладки. Если у установки принято использовать ENROLLMENT tab проекта, создать
или обновить её с безопасными привязками. Не публиковать credentials и приватные абсолютные пути.

Независимо прочитать каталог **коннектором чата**. Сверить точную новую запись с
runtime и неизменность всех прежних записей; тем же коннектором прочитать command
и result tabs. Одной локальной конфигурации/UI или SDK недостаточно для вывода,
что новый чат обнаружит регистрацию и сможет использовать её.

### Доказательства завершения

Сообщить точные project/workspace, репозиторий/ветку, запрошенный канал/target,
безопасную ссылку обнаружения, реальное состояние каналов и оставшиеся ограничения.
Различать **регистрацию runtime/репозитория**, **регистрацию маршрута чата** и
**проверку исполнения задач**. Завершение означает проверку всех частей именно
запрошенного маршрута; иначе назвать недостающую часть и уже выполненные шаги.
Idle DRAFT подтверждает регистрацию, но не запуск/доставку, поддержку guarded
write коннектором или автоматическое получение Drive file-version metadata.
Runner 0.3.0.dev6 сам определяет опущенную версию artifact; при проверке доставки
применять [правило версии из скилла передачи](../skills/usage/chatgpt/deliver-handoff-to-runner/SKILL.ru.md#проверка-реальных-возможностей-передачи),
не требуя от коннектора вернуть это поле.
