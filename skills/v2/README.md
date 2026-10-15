# Repo-local Runner v2 instructions / локальные инструкции

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
