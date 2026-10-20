# Chat-Dev Runner

[English](#english) · [Русский](#русский)

## English

Chat-Dev Runner connects a chat assistant to development projects on your computer
through **Google Docs and GitHub**. Work on a project from chat, run it locally,
and return its output, logs and test reports to the conversation. Projects remain
ordinary local Git repositories that you can open in VS Code and edit yourself.

### What it does

- Creates projects through Google Docs or the local UI, and works with existing repositories.
- Targets a chosen branch and local workspace for each operation.
- Transfers code changes, prepares configured environments, and runs programs and tests.
- Captures source snapshots, logs, test reports and other declared output files.
- Publishes results to Drive or GitHub; v2 can add results to the tested branch.
- Provides a local web interface, persistent history, backup and restore.

### How it works

The chat prepares code and an explicit command in a registered Google Doc or
GitHub command file. The local Runner receives the command, selects the project
and execution profile, freezes the source, runs the requested operation, and
returns a receipt and results. You review the output in chat and decide the next
step. Runner itself does not call a language model or generate follow-up tasks.
The GitHub v2 channel runs tasks for projects that have already been enrolled.

Execution uses either an approved native profile or the configured rootless
Docker profile. Native programs run with your ordinary user permissions;
containers provide the separate execution boundary. Credentials stay outside
repositories, and the web interface listens only on `127.0.0.1:8765`.

### Install

**Linux preview · 0.3.0.dev1.** Requires Python 3.10+, Git, and the tools needed by
your projects. Python, JavaScript, TypeScript and C# recipes have been exercised
on Linux. Windows/macOS, Java, Compose and Unity are not qualified for this release.

The v2 Linux preview is available in `main`:

```bash
git clone https://github.com/ViskyG/chat-dev-runner.git
cd chat-dev-runner
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.production.lock
.venv/bin/python -m pip install --no-deps .
.venv/bin/runner doctor --offline
```

Next, enroll your projects, tool profiles and chosen Google/GitHub channels using
[V2 operations](docs/V2_OPERATIONS.md). Provider setup is currently manual;
examples and historical installation IDs are not ready-to-use account settings.
Keep OAuth credentials and tokens outside the repository with `0600` permissions.
Reuse an existing Drive installation rather than bootstrapping a duplicate.

Once configuration is ready, preview and install the Linux user service:

```bash
.venv/bin/runner install --dry-run
.venv/bin/runner install --user
.venv/bin/runner status
```

The configured service starts with command intake enabled. See
[V2 operations](docs/V2_OPERATIONS.md) for commands and recovery, and
[STATUS.json](STATUS.json) for implemented capabilities and remaining work.

**License:** no open-source license is currently provided for this project.
Bundled dependencies retain their [third-party licenses](THIRD_PARTY_NOTICES.md).

## Русский

Chat-Dev Runner связывает чат-ассистента с проектами на вашем компьютере через
**Google Docs и GitHub**. Можно работать над проектом из чата, запускать его
локально и возвращать в разговор вывод программы, логи и результаты тестов.
Проекты остаются обычными локальными Git-репозиториями: их можно открыть в
VS Code и редактировать самостоятельно.

### Возможности

- Создание проектов через Google Docs или локальный интерфейс, работа с существующими репозиториями.
- Выбор ветки и локальной рабочей папки для каждой операции.
- Перенос изменений кода, подготовка настроенного окружения, запуск программ и тестов.
- Сохранение снимков исходников, логов, отчётов тестов и заданных выходных файлов.
- Публикация результатов в Drive или GitHub; v2 умеет добавлять их в проверенную ветку.
- Локальный веб-интерфейс, история операций, резервное копирование и восстановление.

### Принцип работы

Чат готовит код и явную команду в зарегистрированном Google Doc или командном
файле GitHub. Локальный Runner принимает команду, выбирает проект и профиль
исполнения, фиксирует исходники, выполняет операцию и возвращает подтверждение
и результаты. Вы рассматриваете вывод в чате и решаете, что делать дальше.
Сам Runner не обращается к языковой модели и не создаёт следующие задания.
GitHub-канал v2 выполняет задания для уже подключённых проектов.

Выполнение идёт через разрешённый нативный профиль или настроенный профиль
rootless Docker. Нативные программы работают с правами вашего обычного
пользователя; контейнеры обеспечивают отдельную границу исполнения. Учётные
данные хранятся вне репозиториев, веб-интерфейс доступен на `127.0.0.1:8765`.

### Установка

**Предварительная Linux-версия · 0.3.0.dev1.** Нужны Python 3.10+, Git и инструменты
ваших проектов. На Linux проверены сценарии Python, JavaScript, TypeScript и C#.
Windows/macOS, Java, Compose и Unity пока не прошли приёмку для этой версии.

Предварительная Linux-версия v2 находится в ветке `main`:

```bash
git clone https://github.com/ViskyG/chat-dev-runner.git
cd chat-dev-runner
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.production.lock
.venv/bin/python -m pip install --no-deps .
.venv/bin/runner doctor --offline
```

Затем настройте проекты, профили инструментов и выбранные каналы Google/GitHub по
[инструкции v2](docs/V2_OPERATIONS.md). Настройка провайдеров пока выполняется
вручную; примеры и исторические ID установки нельзя использовать как готовые
настройки своего аккаунта. Храните OAuth credentials и токены вне репозитория с
правами `0600`. Если установка Drive уже существует, подключайте её без создания
второй.

После настройки просмотрите план и установите пользовательский Linux-сервис:

```bash
.venv/bin/runner install --dry-run
.venv/bin/runner install --user
.venv/bin/runner status
```

Настроенный сервис запускается с включённым приёмом команд. Команды и
восстановление описаны в [инструкции v2](docs/V2_OPERATIONS.md), текущее состояние
разработки — в [STATUS.json](STATUS.json).

**Лицензия:** открытая лицензия для проекта пока не предоставляется.
Включённые зависимости сохраняют свои [сторонние лицензии](THIRD_PARTY_NOTICES.md).
