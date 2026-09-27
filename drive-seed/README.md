> This installation already exists. The seed is a reference specification only.
> Do not run bootstrap apply or overwrite existing Docs. See
> `../config/installation.existing.json` and `../docs/LOCAL_SETUP.md`.

# Комплект рабочего пространства Google Drive

Это готовые исходные материалы и установщик **нового** рабочего пространства. Пока они лежат
в архиве, ничего в твоём аккаунте не создано. Установщик по умолчанию лишь показывает план.
Рабочий облачный runner ещё должен быть реализован по пакетам Codex.

## Что будет создано
```text
Chat-Dev Runner/
  START HERE                         Google Doc, руководство
  INSTALLATION INDEX                 Google Doc, реальные ID после установки
  00 Control/
    CHAT PROTOCOL                    Google Doc, инструкции чату
    COMMAND                          Google Doc, единственная команда установки / DRAFT
    RECEIPT                          Google Doc, ответ runner
    SYSTEM STATE                     Google Doc, epoch и возможности / NOT_CONFIGURED
    COMMAND RECIPES                  Google Doc, примеры
  01 Projects/                       будущие проекты и рабочие области
  02 Results/                        будущие неизменяемые результаты
  03 Contracts/                      JSON Schema + типы выходных данных
  04 Templates/                      образцы, не отслеживаемые команды
```
Новый проект добавляет собственные PROJECT STATE / CONTEXT и workspaces. Каждая область получает
собственные COMMAND / CHANGESET / RECEIPT / LATEST RESULT. Они создаются локальным OAuth-приложением;
чат редактирует уже зарегистрированные документы. Общий COMMAND используется для создания проекта,
а не как бесконечный список операций.

## Посмотреть план без подключения к Google
Из корня полного архива или отдельного Drive-kit:
```bash
python3 scripts/bootstrap_drive.py --plan
```
Никаких учётных данных для этой команды не нужно.

## Создать пространство позднее
Нужно локально создать OAuth **Desktop app** в своём Google Cloud project, включить Drive API и
Docs API, настроить consent/test users согласно типу проекта и сохранить client JSON вне репозитория.
Точные параметры своего аккаунта проверь по текущей официальной документации; не размещай токены
в чате. Эти действия и сквозной canary-тест описаны в пакете EP-04.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[bootstrap]'
python scripts/bootstrap_drive.py --apply \
  --credentials "$HOME/.config/chat-dev-runner/google-client.json"
```
Откроется локальная OAuth-авторизация. Сценарий использует полный Drive scope, создаёт собственную папку
и документы, сохраняет точные ID в `.local-state/drive-bootstrap.json`. Состояние и refresh token
не попадают в архив или Git. Повторный запуск использует installation ID и appProperties, а не
ищет «похожую папку» и не перезаписывает редактированные документы. Не удаляй local state перед
повторным запуском: новый installation ID означает новую установку.

Если используются только файлы из отдельного Drive-kit без pyproject, зависимости можно поставить
из его `requirements-bootstrap.txt`. Полный архив содержит оба варианта.

## Что НЕ произойдёт
Установщик не включает runner, не меняет sharing, не создаёт GitHub-репозиторий, не запускает тесты
и не выставляет READY. Неизвестные реальные ID/политики не подменяются догадками. Нет подключения
к интерфейсу ChatGPT. Облачный bootstrap в этой среде не выполнялся; подтверждены только план,
локальные проверки и структура исходных файлов.

## После реализации runner
Локальный мастер регистрации привязывает created IDs к конфигурации и выполняет canary: чат
читает/редактирует безвредный документ, runner видит изменение. Только затем владелец включает
приём команд. Новый `epoch` публикуется в SYSTEM STATE. Примеры с нулевым epoch, просроченной
датой и DRAFT никогда не превращаются в команды сами.

Проверка доступа к документу выполняется по точному ID и не следует из одного наличия
токена. Для существующей установки bootstrap запрещён; этот каталог остаётся справочным
описанием структуры. Владелец отдельно разрешил полный Drive scope для выделенного аккаунта.
