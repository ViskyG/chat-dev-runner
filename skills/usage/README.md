# Chat-Dev Runner agent skills / скиллы использования Runner

This directory contains **public distribution copies** of selected owner-maintained skills from `ViskyG/agent-skills`. Use them to guide an agent through preparing a compatible project, submitting one finite task, reading results or coordinating an authorized sequence of tasks. These files do **not** install agents, register projects, grant permissions or start a service. The installed Runner and its [v2 protocol](../v2/operate-runner-v2/SKILL.md) remain the source of truth for command and result formats.

| Purpose / назначение | Skill / скилл | Russian / перевод |
| --- | --- | --- |
| Execute and inspect from ordinary ChatGPT / прямой запуск | [operate-runner-from-chat](chatgpt/operate-runner-from-chat/SKILL.md) | [Русский](chatgpt/operate-runner-from-chat/SKILL.ru.md) |
| Integrate multi-step work via an existing Udot coordinator / координация через Udot | [execute-roadmap-via-chat-runner](udot/execute-roadmap-via-chat-runner/SKILL.md) | — |
| Make a repository Runner-compatible / подготовка проекта | [prepare-runner-project-v2](../v2/prepare-runner-project-v2/SKILL.md) | [Русский](../v2/prepare-runner-project-v2/SKILL.ru.md) |
| Read/write the real command protocol / протокол команд | [operate-runner-v2](../v2/operate-runner-v2/SKILL.md) | [Русский](../v2/operate-runner-v2/SKILL.ru.md) |
| Install or upgrade Runner / установка или обновление | [install-runner-v2](../v2/install-runner-v2/SKILL.md) | [Русский](../v2/install-runner-v2/SKILL.ru.md) |

## Which one do I need? / Что выбрать?

**One-off ChatGPT task:** start with `operate-runner-from-chat` if you have an **already enrolled project and mailbox**. It refers to `operate-runner-v2` for the exact wire protocol. For a new project, use `prepare-runner-project-v2` and the [task examples](../../examples/v2/README.md) first.

**Udot:** `execute-roadmap-via-chat-runner` describes a **larger workflow** that uses a separate Udot coordinator, transit storage and shared workflow-state skills. Those dependencies are **not** distributed here. Reading the skill is not a complete Udot installation.

**Обычный ChatGPT:** сначала подготовьте проект через `prepare-runner-project-v2`, затем для конечной задачи используйте `operate-runner-from-chat`. Для Udot потребуются отдельный координатор и его общие инструкции.

## Provenance / происхождение

[PROVENANCE.json](PROVENANCE.json) records exact upstream source identities and blob SHAs. The public copies adjust local links, clarify setup and replace inaccessible private-test links with published release evidence. The canonical skill library remains the maintenance source; this repository distributes **usable copies**, not a second independent standard. Check the **deployed** Runner version before applying version-specific instructions.

[Back to README](../../README.md#english) · [Вернуться к README](../../README.md#русский)
