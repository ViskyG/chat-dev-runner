# Repo-local Runner v2 instructions / локальные инструкции

## ChatGPT and Udot usage / работа из ChatGPT и Udot

The [public usage skills](../usage/README.md) show how an ordinary ChatGPT chat submits and checks a finite command, or how an existing Udot workflow coordinates larger waves. These are **distribution copies** of the owner’s canonical skills, not newly installed agents. The v2 operational protocol below remains authoritative.

[Публичные скиллы](../usage/README.md) описывают прямой запуск из обычного ChatGPT и координацию этапов через Udot. Это копии исходных инструкций, **не автоматическая установка**. Действующий протокол команд и результатов определяет Runner v2.

**SOURCE_PREVIEW — not installed or globally registered / не установлены и не
зарегистрированы глобально.** Read one matching English entrypoint or its full
Russian companion; loading all three is unnecessary.

| Request / запрос | Entry / вход | Русский текст |
| --- | --- | --- |
| Install or upgrade / установка, обновление | [install-runner-v2](install-runner-v2/SKILL.md) | [Перевод](install-runner-v2/SKILL.ru.md) |
| Prepare a project / подготовка проекта | [prepare-runner-project-v2](prepare-runner-project-v2/SKILL.md) | [Перевод](prepare-runner-project-v2/SKILL.ru.md) |
| Submit/read/control a task / отправка, результат, управление | [operate-runner-v2](operate-runner-v2/SKILL.md) | [Перевод](operate-runner-v2/SKILL.ru.md) |

[Operations and concrete fields](../../docs/V2_OPERATIONS.md) explain the actual
runtime, service, mailbox and result interfaces. The
[runtime example](../../config/runtime-v2.example.json) is inert; its IDs and
paths must be replaced from enrollment and it contains no READY command.

These are adaptations of the verbatim design imports already preserved in
[design/github-native-v2/skills](../../design/github-native-v2/skills/README.md).
[PROVENANCE.json](PROVENANCE.json) pins their original paths and SHA-256 values.
The source files were not modified. Current APIs and the owner's accepted
source-stage decisions determine these adaptations; historical design wording
is not fresh deployment authorization.

Creating these local files does not change canonical `ViskyG/agent-skills`,
navigation, global skill discovery, AGENTS, installed service or account grants.
Deployment acceptance and any future skill-library registration are distinct
operations with their own actual authorization and evidence.
