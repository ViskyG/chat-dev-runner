# Chat-Dev Runner v2 — дорожная карта интеграции DELIVER_HANDOFF

**Редакция:** 0.2.0 / 2026-10-22  
**Статус:** готовая к передаче Codex интеграционная дорожная карта; НЕ свидетельство установленной функции.  
**Кодовая база:** `ViskyG/chat-dev-runner@d1e930a5ebdf87f5ab64fb70914420b6d18db03b`. Проверить HEAD при старте, не считать исторический commit текущим навсегда.  
**Исходный комплект:** `runner-project-handoff-kit.zip`, `handoff_reference/` — локально проверенное автономное ядро, а не готовый адаптер Runner.

## 0. Цель и границы

**Одна законченная возможность:** обычный ChatGPT формирует архив проектирования, дорожной карты или несколько отдельных файлов; публикует их как обычные файлы в заранее разрешённой папке Google Drive; через зарегистрированный *v2* Google Docs command tab публикует `DELIVER_HANDOFF`. Один существующий локальный Runner по точным ID загружает байты, проверяет их, сохраняет в **новую, уникальную папку входящих зарегистрированного проекта** и возвращает отдельное подтверждение доставки и пригодный для копирования Codex prompt.

```text
ChatGPT → исходные бинарные файлы в разрешённой папке Drive
        → существующий Docs v2 slot: DRAFT → guarded READY
        → Runner v2: ACK (принято) → операция доставки
        → <registered workspace>/.runner/handoffs/<handoff_id>/
        → terminal delivery result + ссылка/текст инструкции Codex
        → владелец самостоятельно поручает Codex работу
```

**Не входит в эту задачу:** запуск программ и тестов, изменение исходного кода проекта, исполнение ZIP, автоматический вызов Codex, запуск второго Runner, `git commit`/`push`/PR, новый общий конвейер архивов или новая очередь. Подключение по одобренному владельцем `target_id`, не через введённый чатом абсолютный путь. Google Docs является каналом команды; обычный ZIP хранится в Google Drive и скачивается Runner.

**Правила нынешнего v2:** `cdr-mailbox/2`, токен слота, условное изменение DRAFT→READY и раздельные ACK/terminal state. v2 допускает FIFO с одной lane; устаревший запрет очереди из исторического v1 сюда не переносить. Действующая инструкция — `docs/V2_OPERATIONS.md`. Чужие действующие v1 ресурсы, старый Drive root/Index, токены и профиль исполнения не менять.

**Принцип эффективности:** отладка по самому узкому неопровергнутому свойству, не запуск полного pytest после каждой правки. Повторные сохранённые доказательства считать применимыми, пока затронутый контракт не изменился. Ни один mock не заменяет обязательный живой этап.

## H0. Контракт и привязка получателя — минимальный fake-backed end-to-end

**Результат пакета:** приём v2-команды `DELIVER_HANDOFF` доходит до отдельного обработчика, но никакой внешний файл ещё не скачивается и на настоящий пользовательский проект ничего не пишется.

**Обязательные изменения:**

1. Сверить текущие `AGENTS.md`, `STATUS.json`, `docs/V2_OPERATIONS.md`, `schemas/v2-mailbox.schema.json`, `runner_app/config_v2.py`, `domain/contracts_v2.py`, `engine/intake_v2.py`, `storage/intake_v2.py`, `engine/runtime_v2.py`, `engine/queue_v2.py`, `engine/task_v2.py`, `engine/docs_v2.py`, `git/source_v2.py`. Отметить только фактический drift относительно `d1e930a5`.
2. Добавить в **owner-local enrollment** опциональное соответствие `project_id → target_id → зарегистрированная постоянная локальная workspace`. Путь входящих фиксируется локальным правилом (по умолчанию `.runner/handoffs/`), а список разрешённых Drive parent folder IDs и лимиты — конфигурацией владельца. Команда не может изменить путь, папку-источник, разрешения или профили. Проект, зарегистрированный **только** как GitHub source без локальной рабочей области, до регистрации `target_id` должен получить понятный отказ.
3. Добавить `DELIVER_HANDOFF` как совместимое условие существующей схемы `cdr-mailbox/2`, с валидацией *вложенного* `cdr-handoff/1`. Существующие формы `RUN_TASK`, `SHELL`, `CANCEL`, `STATUS`, `REDELIVER` оставить неизменными. Не заменять исходную mailbox-схему референсным JSON-fragment.
4. В `V2Assembly` подключить **точечный диспетчер**: для `DELIVER_HANDOFF` — новый handoff handler; для остальных действий — существующий `NativeTaskRunner`. Использовать действующие `V2Service`, `IntakeStore`, `TaskOutcome` и terminal observer. Не отправлять handoff в ветки `_execute()`/`_execute_container()` и не требовать execution/native profile. Доставка — обычная очередь v2, не новый канал привилегий.
5. Добавить исключение `.runner/handoffs` в `git/source_v2.py` и в применимые пути source/context snapshot, включая dirty/untracked handling. Это не равно автоматическому изменению `.gitignore`: если новый inbox виден в `git status`, сообщать об этом явно. Не редактировать `.gitignore` и `.git/info/exclude` самовольно. HEAD, индекс и отслеживаемые файлы сохранять.
6. Не делать автоматически выдачу OAuth/Google scopes, создание второго Drive root, регистрацию глобальных скиллов или изменение установленной службы.

**Фокусные проверки (без сети):** валидный manifest; несовпадение `project_id` с mailbox; неизвестный `target_id`; проект только с GitHub; попытка задать absolute path; неподтверждённая source folder; старые 5 команд по-прежнему валидны; уникальная область (другая workspace недоступна); `.runner/handoffs` исключён из заморозки source; обработчик не вызывает `NativeTaskRunner` и не создаёт никаких provider effects при отказе.

**H0 DONE, когда:** зарегистрированный локальный fake mailbox принимает новый DRAFT/READY по правилам v2; проект/target проходят **локальную** проверку; новый request приводит к ожидаемому `TaskOutcome` (временно `PROVIDER_NOT_WIRED` или `NOT_CONFIGURED`), не создавая программу/контейнер/вторую очередь. Написан `checkpoints/H0.md` с точными файлами и результатами.

## H1. Загрузка, безопасное размещение и завершённый receipt — рабочее ядро

**Результат пакета:** по принятой операции реальные переданные байты попадают в зарегистрированный inbox; формируется неизменяемая история назначения и доставленная инструкция; ack больше не путается с terminal result.

**Обязательные изменения:**

1. Использовать `handoff_reference/contracts.py` и `handoff_reference/delivery.py` как начальную проверенную реализацию. Если актуальные служебные функции безопасности Runner строже, применять их вместо дублирования. Сохранить `TargetBinding`, `BlobProvider` и точную семантику `handoff_id + manifest_digest`.
2. Добавить **узкий Google Drive raw-file provider**: `files.get` metadata по точному `drive_file_id`, далее ограниченное media-stream download. Проверять parent ID из owner enrollment, mime-type **обычного файла** (не нативный Google Doc), version, size, тип, фактический SHA-256. Повторить метаданные после download и отклонить подмену версии/родителя. Никакого URL fetch из команды, публичного доступа или автоматического export Docs.
3. Установить реальные per-request HTTP timeout, overall deadline, limit отдельных байтов и общего размера, ограничение числа файлов, zip-entry count, unpacked bytes/depth и свободного диска. Счётчик времени между блоками **не** заменяет таймаут блокирующего provider-call. Поддерживать отмену до локального commit; завершение после commit не представлять как отменённую доставку.
4. Под существующим owner-controlled lock/staging создать отдельный `.pending-*`, сохранить оригиналы byte-for-byte и **опционально** распаковать только обычные ZIP-файлы в `content/<artifact_id>`. Запретить абсолютные/`..` пути, дубликаты имён (включая case folding), symlink/hardlink/device/FIFO, неожиданные расширения и попытку перезаписать готовый ID. Создать `START_HERE.md`, `CODEX_PROMPT.txt`, `manifest.json`, `delivery.json` с реальными путями и SHA; выполнить flush/fsync и единственный no-replace rename. Никогда не извлекать файлы поверх корня проекта.
5. До внешнего I/O вести durable record намерения через механизм v2; после успешного rename фиксировать local effect. При аварии **между rename и записью в DB** восстановление сверяет `delivery.json`, digest и целостность, не скачивая/переименовывая поверх готового пакета. Идентичный `handoff_id`+digest должен быть идемпотентен; иной digest с тем же ID — terminal conflict. Не превращать неопределённую запись в безусловный retry.
6. В `TaskOutcome` и хранимый v2 state включить строго типизированный неисполнительный результат: `handoff_id`, `project_id`, `target_id`, `manifest_sha256`, `local_delivery_status`, `files_count`, `safe_relative_handoff_path`, `codex_entrypoint`, `delivery_receipt`. Не выдумывать `run_id`, `tested_commit`, test_count, quality_gate, GitHub PR. Абсолютный путь можно показывать только владельцу в аутентифицированном локальном UI или готовом локальном prompt, а не в публичном remote status.
7. **Отдельно завершить обратную связь для Docs-origin**: текущий v2 Docs слот подтверждает приём, но один только ACK недостаточен. При необходимости зарегистрировать локально exact-ID `handoff result Doc/tab` и создать version/revision-guarded publisher для terminal status; оставить ACK управляемым текущим mailbox. Не писать в чужой Docs tab и не подменять `receipt` поле неподписанным текстом. Provider-write unknown остаётся видимым `DELIVERY_RESULT_UNCONFIRMED`, а локально подтверждённые файлы не удаляются из-за неудачного сообщения чату.
8. Документировать результат передачи отдельно от будущего исполнения Codex. Существующая v2 единая очередь и системные permissions не меняются.

**Проверки:** fake metadata version/parent change; malformed MIME; обрыв/timeout/дисковый лимит; ZIP traversal/symlink/double path/zip bomb; cancel before and after rename; crash after local rename before ledger completion; одинаковый повтор без повторного download; одноимённый другой пакет как конфликт; неуспешная публикация terminal Docs receipt без потери locally delivered; отсутствие новых tracked/HEAD/index changes; действующие v2 RUN_TASK и STATUS по-прежнему работают.

**H1 DONE, когда:** один test-file из fake-провайдера, поданный через всю собранную v2 intake/handler цепочку, появляется в новой папке с точными байтами и receipt, а `READY/ACK` не используется как подтверждение доставки. Offline assembled gate закрыт. Реальный Google ещё не считается пройденным. checkpoint `checkpoints/H1.md`.

## H2. Скилл, операционная инструкция и ровно один живой сквозной пример

**Результат пакета:** полностью воспроизводимое использование через подключённый чат и установленный Runner без ручного раскладывания архива; в отсутствие нужных прав честный статус BLOCKED и проверенный fallback.

1. Обновить встроенную пользовательскую документацию, используя `skills/deliver-handoff-to-runner/SKILL.md` из комплекта. Проверять у реального чата доступные операции upload Drive и **условного** изменения существующего Docs tab. Не выводить capability из того факта, что Google Drive подключён. Канонический скилл вне исходного репозитория не переписывать без отдельной явной команды владельца; сначала распространять reviewable copy в текущей структуре `skills/usage`.
2. Перед загрузкой получить действительные registered `project_id`, `target_id`, разрешённый Drive folder ID и mailbox Doc/tab + slot token. В скилле рассматривать ZIP и несколько файлов как один пакет, вычислять size/SHA, загружать обычные Drive files и делать независимый metadata readback. Затем **один** guarded DRAFT и **один** guarded READY с реальным актуальным token. На конфликте не подменять чужой незавершённый запрос и не создавать невидимую очередь.
3. Получить и сопоставить **оба** факта: ACK с accepted slot token / request ID, затем terminal `DELIVERED` для того же request и `handoff_id`, либо явный отказ/unknown. Владелец получает готовый `CODEX_PROMPT.txt` и фактический путь пакета. В status ответе отличать `UPLOADED_TO_DRIVE`, `ACCEPTED_BY_RUNNER`, `DELIVERED_LOCAL`, `REMOTE_RECEIPT_CONFIRMED`.
4. Один короткий live gate на **одноразовом зарегистрированном тестовом проекте** с существующей dirty sentinel: передать ZIP с roadmap и ещё один текстовый файл; проверить hash оригиналов, распаковку, инвентарь, кросс-чат receipt и фактические файлы, Git HEAD/index/tracked source, отсутствие запуска скриптов, PR и второго root. На втором обращении читать сохранённый результат, не повторять реальную загрузку только ради уверенности.
5. По желанию добавить мини-секцию локального UI «Входящие»: список пакетов, статус, безопасный относительный путь, кнопка копирования Codex prompt. Это **последний и неблокирующий** косметический пункт: HTML из архива не отображать как доверенный same-origin код. Если нет квоты, скилл+полученный путь являются полноценным MVP.
6. Собрать один release candidate по действующему владельческому backup/upgrade процессу, проверить upgrade/rollback и сохранность старых v1/v2 проектов. Один оправданный полный regression после source freeze, а не после каждого файла. Для живого smoke запросить разрешение только если конкретного разрешения действительно не хватает.

**H2 DONE, когда:** реальный чат (или явно маркированная ручная первая нога) передал пакет через Drive→Docs→Runner и получил подтверждённый terminal result; готовая инструкция открывает реальный `START_HERE.md`; пользовательские Git-файлы и чужие workspace не менялись; работа записана в `checkpoints/H2.md`. Если инструмент чата не умеет upload/guarded Docs write, отметить `CHAT_TRANSPORT_BLOCKED`, но не утверждать, что автоматический end-to-end проверен.

## Проверки по ценности (вместо бесконечных полных прогонов)

| Когда | Минимальное подтверждение | Чего НЕ делать |
|---|---|---|
| После контракта / enrollment | Schema + authorization focused tests | Не пробовать Google API |
| После сборки H0 | fake DRAFT/READY → handler, отказ по scope | Не гонять весь репозиторий |
| После file-core H1 | ZIP + Git dirty/index preservation + crash cuts | Не повторять исторические 63 теста при неизменном коде |
| После Drive adapter H1 | fake Google metadata + partial stream + readback | Не отправлять 10 live ZIP для отладки |
| После receipt H1 | lost ACK/terminal write + retry read-only | Не путать ACK со скачиванием |
| После H2 source freeze | один justified full v2 suite + lint/type/build | Не переигрывать полный EP-07 |
| После утверждённого rollout | один live short inert ZIP, второе чтение результата | Не импортировать продакшен-проекты для canary |

## Критерии остановки / эскалации

- Не удаётся подтвердить идентичность постоянной workspace, owner enrollment или Drive folder → `TARGET_NOT_ENROLLED`, не создавать произвольную папку.
- Недоступно загрузить файл из чата → можно сделать **явно ручную** загрузку, но маркировать её `MANUAL_UPLOAD`, а не end-to-end automatic.
- Нет guarded Docs update → оставить DRAFT, не сымитировать READY через `SHELL` или неуправляемый браузер.
- После скачивания исходный Drive version/parent изменился → отказ и удаление только временного stage, не старой доставки.
- Потеряны обстоятельства после rename → read-only reconciliation receipt/inventory; не перезаписывать destination.
- Получен неизвестный новый HEAD репозитория → сначала сравнительная ревизия только затронутых интерфейсов; не пересоздавать проект или план.

## Экономный запрос Codex

> Integrate the additive `runner-handoff-extension` kit into the existing `ViskyG/chat-dev-runner` v2. The reviewed baseline is `d1e930a5`; inspect current main and only relevant drift. Read `integration/ROADMAP.md` and the kit's `handoff_reference`, schemas and tests. Start with H0 and proceed to H1/H2 only as gates pass. Reuse the single Runner service, `cdr-mailbox/2`, token-guarded DRAFT→READY, v2 FIFO, project enrollment and existing private Drive installation. Port/reuse tested code rather than redesign it. Keep delivery separate from task execution and source mutation; produce a terminal handoff receipt distinct from ACK. Test narrowly while editing and make one full pass at source freeze. Do not treat examples as live permissions, do not create a second Drive root, and do not claim a cloud canary that did not run. Stop with exact checkpoint on real permission blockers.

## Критерий завершённости продукта

Оператор в обычном чате создаёт ZIP дорожной карты → отдаёт его через разрешённый Drive folder → публикует `DELIVER_HANDOFF` в существующем Docs v2 слоте → Runner размещает ZIP и, если указано, безопасную распакованную копию в зарегистрированном inbox → возвращает отдельный терминальный `DELIVERED` → владелец копирует короткую инструкцию Codex, который читает точный `START_HERE.md` и работает уже в своём обычном проекте. Ни один из этих этапов не требует ручного копирования файлов при доступности нужных подключённых операций.
