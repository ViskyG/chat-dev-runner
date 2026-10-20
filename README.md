# Chat-Dev Runner

**Let ChatGPT run your project's code on your own computer — and read the real results.**

**Идея:** дать обычному чату ChatGPT возможность запускать код вашего проекта на вашем компьютере и видеть, что получилось.

[English](#english) · [Русский](#русский)

---

## English

### The missing half of coding with ChatGPT

ChatGPT can write a function, suggest a fix, and prepare tests. But what happens next? **Does the code actually work in your project, with your dependencies, on your computer?**

Usually you have to copy code out of the conversation, run it in a terminal, copy the errors back, and repeat the process after every change.

**Chat-Dev Runner closes that gap.** It is a small local service that lets an ordinary ChatGPT conversation request *approved* development tasks on your own machine. The chat sends a command through GitHub or Google Docs; Runner runs it locally and makes the real logs, test reports, and declared outputs available for the chat to read.

The goal is straightforward: **write in chat → run on your computer → inspect the result in chat → improve the code.** No guessing whether generated code passed a test.

### A real example

Imagine you're working on a Python project.

1. ChatGPT prepares a code change and a test in a GitHub branch.
2. You ask: *“Run this branch's tests with Chat-Dev Runner and tell me what failed.”*
3. Runner, already running on your Linux computer, receives the request and runs the **registered task** using your approved Python environment.
4. Logs and test reports are published back to GitHub.
5. ChatGPT reads the **actual error**, explains it, and can prepare a fix. You can then request another run.

We verified this round trip on a real local machine: **7 passing tests → 1 deliberately failing test → 8 passing tests after a fix**. Those results came from execution, not a simulated answer.

Runner **does not call an AI model** or automatically create an endless sequence of follow-up prompts. Further work is initiated from the conversation or another already approved workflow.

### How it works

```text
ChatGPT conversation
        |
        | writes a specific, finite request
        v
GitHub command file (or registered Google Doc)
        |
        | read by the service on your machine
        v
Chat-Dev Runner -> approved project + task -> local execution
        |
        | receipt, logs, test reports, output files
        v
GitHub results (or configured delivery channel)
        |
        | ChatGPT reads the evidence
        v
Same conversation: diagnose, decide, improve
```

**GitHub carries the request and the evidence; your computer actually runs the code.** For a GitHub v2 run, results are added under `.runner/results/<runner>/<request>/` on the **tested source branch**. Runner records the source commit and frozen workload fingerprint, so you can tell exactly which code was tested.

An acknowledgement means *“Runner accepted this request”*, **not** *“the tests passed.”* The chat must read the matching execution and test results. The command-file branch may differ from the branch being tested.

GitHub-only v2 needs no Google Drive installation. Registered local workspaces and the existing Google Docs workflows are also supported in their respective configurations.

### What can it do?

- Run registered tests, builds, or other finite project tasks using the configured tools on your computer.
- Return stdout/stderr, test failures and counts, tool/environment information, and declared output files.
- Work against a selected Git branch, preserving its exact tested snapshot.
- Keep results of old runs while new code is tested again.
- Display request history and evidence in a local web interface.

Runner is **not an unrestricted remote terminal**. You enroll projects, choose allowed execution profiles and define tasks and outputs in `.runner/project.json`. Shell actions are a separately enrolled capability.

### Getting started: Linux preview

**Current release scope:** Linux preview `0.3.0.dev1`, with manual setup rather than one-click onboarding. You need Linux, Python 3.10+, Git, the tools for your project, and a GitHub repository. For ChatGPT to submit requests through GitHub, its connected GitHub app must have suitable access to the selected repository.

**1 — Install the code:**

```bash
git clone https://github.com/ViskyG/chat-dev-runner.git
cd chat-dev-runner
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.production.lock
.venv/bin/python -m pip install --no-deps .
.venv/bin/runner doctor --offline
```

**2 — Set up your project and permissions.** Follow the [Runner v2 operations guide](docs/V2_OPERATIONS.md) to register the GitHub command mailbox, your repository, permitted task, tools, and execution profile. See the [configuration example](config/runtime-v2.example.json) and [small project recipes](examples/v2/README.md). Their example IDs and paths are **placeholders**, not settings to use unchanged. Keep tokens and credentials outside Git.

**3 — Install and start the configured Linux user service:**

```bash
.venv/bin/runner install --dry-run
.venv/bin/runner install --user
.venv/bin/runner status
```

**4 — Ask ChatGPT to run a registered task.** After your real project and mailbox have been enrolled, you might say:

> Use Chat-Dev Runner through GitHub to run my project's registered `test` task on branch `feature/parser`. Check the live command slot first and don't duplicate an accepted request. Read the matching result, tell me which tests failed, and link to the reports.

Replace the branch and project details with your real setup. ChatGPT needs GitHub access that supports **revision-guarded mailbox writes**. A connected repository or a command in `DRAFT` is not enough to trigger execution.

Results appear on the tested branch. Look for `summary.md`, `tests.json`, `result.json`, `source.json`, and the captured logs/reports. For safe retries, cancellation, and recovery, consult the [operations guide](docs/V2_OPERATIONS.md).

### Trust and current limitations

**No exposed desktop or public inbound port is required.** Runner's local web interface listens on `127.0.0.1:8765`; it communicates through configured providers such as GitHub. It does not require an OpenAI API key or its own language-model service.

**Native execution is not a sandbox.** Code and repository scripts run with your ordinary operating-system user's permissions. Only enroll projects, contributors, tasks, and profiles you trust. A separately configured rootless Docker backend supports a constrained Python test profile, not arbitrary container execution.

Python, JavaScript, TypeScript, and C# project recipes have been exercised on Linux. **Windows/macOS, Java, Docker Compose, and Unity are not qualified** for this preview. Some early errors or failed deliveries may require checking Runner's local status. Read [current implementation status](STATUS.json) for exact scope, not a promise of universal compatibility.

**License:** the project's own code currently has **no open-source license**. Dependencies keep their [third-party licenses](THIRD_PARTY_NOTICES.md).

---

## Русский

### Недостающая половина работы с ChatGPT

ChatGPT умеет писать код, предлагать исправления и создавать тесты. Но что дальше? **А работает ли написанное в вашем проекте, с вашими библиотеками, на вашем компьютере?**

Обычно приходится копировать код из чата, открывать терминал, запускать программу, собирать ошибки и переносить их обратно в разговор. После каждого исправления всё повторяется.

**Chat-Dev Runner создан, чтобы убрать этот разрыв.** Это локальный сервис, через который обычный чат ChatGPT может поручать **разрешённые вами** операции с проектами на вашем компьютере. Чат передаёт команду через GitHub или Google Docs, Runner выполняет её локально, а настоящие логи, отчёты тестов и нужные выходные файлы становятся доступны для анализа в чате.

Главная идея: **написать код в чате → запустить на своём компьютере → увидеть настоящий результат в чате → исправить и проверить снова.**

Не «ИИ уверен, что всё должно работать», а реальный запуск.

### Как это выглядит на практике

Допустим, вы разрабатываете проект на Python.

1. ChatGPT готовит изменение кода и тест в ветке GitHub.
2. Вы пишете: *«Запусти тесты этой ветки через Chat-Dev Runner и объясни, что сломалось».*
3. Runner, уже работающий на вашем Linux-компьютере, получает запрос и запускает **зарегистрированную задачу** в разрешённом Python-окружении.
4. Логи и отчёты тестов публикуются обратно в GitHub.
5. ChatGPT читает **реальную ошибку**, разбирает её и может подготовить исправление. Затем вы просите повторить запуск.

Мы проверили такой цикл на работающей локальной машине: **7 успешно пройденных тестов → 1 намеренно проваленный → 8 успешных после исправления**. Это были настоящие запуски с отчётами, а не имитация ответа.

Runner **сам не обращается к языковой модели** и не запускает бесконечную цепочку заданий. Следующий шаг начинается с поручения в чате или другого уже разрешённого процесса.

### Что происходит между чатом и компьютером

```text
Чат ChatGPT
    |
    | записывает конкретное конечное задание
    v
Командный файл GitHub (или зарегистрированный Google Doc)
    |
    | локальный сервис читает команду
    v
Chat-Dev Runner -> разрешённый проект и задача -> запуск на вашем ПК
    |
    | подтверждение, логи, отчёты и выходные файлы
    v
GitHub (или настроенный канал результатов)
    |
    | ChatGPT читает фактический результат
    v
Тот же чат: разобрать, решить, исправить
```

**GitHub передаёт команды и результаты, а код исполняет ваш компьютер.** В GitHub-канале v2 результаты добавляются в `.runner/results/<runner>/<request>/` той **ветки исходников, которая проверялась**. Runner сохраняет точный коммит и контрольную сумму зафиксированного набора файлов, чтобы было понятно, какой именно код дал результат.

Подтверждение означает *«Runner принял задание»*, а **не** *«тесты прошли»*. Чат должен отдельно прочитать результат выполнения и проверки. Ветка командного файла может отличаться от ветки исходников.

Для GitHub-only v2 **Google Drive не нужен**. Также существуют сценарии с зарегистрированными локальными рабочими папками и прежними каналами через Google Docs.

### Что умеет Runner

- Запускать зарегистрированные тесты, сборку и другие конечные задачи проекта с использованием настроенных инструментов компьютера.
- Возвращать stdout/stderr, сведения о сбоях и числе тестов, версии инструментов, параметры среды и заявленные выходные файлы.
- Проверять выбранную ветку Git и сохранять сведения о фактически проверенных исходниках.
- Сохранять прежние отчёты, даже когда вы исправляете код и повторяете тест.
- Показывать историю запросов и результаты через локальный веб-интерфейс.

Runner — **не бесконтрольный удалённый терминал**. Владелец заранее регистрирует проекты, разрешённые профили исполнения и задачи с выходными файлами в `.runner/project.json`. Команды оболочки доступны только при отдельном разрешении.

### Как начать: предварительная версия для Linux

**Текущее состояние:** Linux preview `0.3.0.dev1`; первоначальная настройка пока **ручная**, а не одним нажатием. Понадобятся Linux с Python 3.10+ и Git, инструменты вашего проекта и репозиторий GitHub. Для отправки команд через GitHub у подключённого к ChatGPT приложения должен быть необходимый доступ к выбранному репозиторию.

**1 — Установите код:**

```bash
git clone https://github.com/ViskyG/chat-dev-runner.git
cd chat-dev-runner
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.production.lock
.venv/bin/python -m pip install --no-deps .
.venv/bin/runner doctor --offline
```

**2 — Настройте проект и разрешения.** По [эксплуатационной инструкции Runner v2](docs/V2_OPERATIONS.md) зарегистрируйте командный файл GitHub, репозиторий, задачу, пути к инструментам и профиль исполнения. Есть [пример конфигурации](config/runtime-v2.example.json) и [небольшие примеры задач](examples/v2/README.md). Идентификаторы и пути в примерах — **заглушки**, не используйте их как готовые настройки. Токены и учётные данные храните вне Git.

**3 — Установите и запустите настроенный пользовательский Linux-сервис:**

```bash
.venv/bin/runner install --dry-run
.venv/bin/runner install --user
.venv/bin/runner status
```

**4 — Попросите ChatGPT запустить зарегистрированную задачу.** Когда реальный проект и его командный файл настроены, можно написать:

> Через Chat-Dev Runner и GitHub запусти зарегистрированную задачу `test` моего проекта на ветке `feature/parser`. Сначала проверь текущее состояние командного файла; не отправляй повторно уже принятое задание. Потом прочитай результат, расскажи, какие тесты упали, и дай ссылки на отчёты.

Подставьте свои данные проекта и ветки. Чату нужен доступ к GitHub с возможностью **условной записи с проверкой версии файла**. Само подключение репозитория или подготовка команды в состоянии `DRAFT` ещё ничего не запускает.

После выполнения ищите в проверенной ветке `summary.md`, `tests.json`, `result.json`, `source.json`, логи и отчёты. Правила безопасного повторного запуска, отмены и восстановления описаны в [инструкции v2](docs/V2_OPERATIONS.md).

### Безопасность и ограничения

**Ваш компьютер не нужно открывать для входящих соединений из интернета.** Локальный веб-интерфейс Runner работает на `127.0.0.1:8765`, а обмен идёт через настроенные каналы, например GitHub. Runner не требует OpenAI API-ключа или собственного сервиса языковой модели.

**Нативный запуск — не песочница (sandbox).** Код и скрипты репозитория выполняются с правами вашего обычного пользователя ОС. Подключайте только доверенные проекты и предоставляйте доступ лишь нужным участникам, задачам и профилям. Отдельно настраиваемый rootless Docker поддерживает ограниченный профиль Python-тестов, а не произвольный запуск любых контейнеров.

На Linux проверены сценарии Python, JavaScript, TypeScript и C#. **Windows/macOS, Java, Docker Compose и Unity пока не прошли приёмку** для этой версии. Некоторые ранние ошибки и сбои доставки могут потребовать проверки локального статуса Runner. Точные подтверждённые возможности и ограничения указаны в [STATUS.json](STATUS.json).

**Лицензия:** у самого проекта пока **нет открытой лицензии**. Сторонние компоненты сохраняют условия [своих лицензий](THIRD_PARTY_NOTICES.md).
