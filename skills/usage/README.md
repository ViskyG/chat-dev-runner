# Chat-Dev Runner agent skills / скиллы использования Runner

This directory contains **public distribution copies** of selected owner-maintained skills from `ViskyG/agent-skills` and a repository-local adaptation of the supplied handoff reference kit. Use them to guide an agent through preparing a compatible project, submitting one finite operation, reading results or coordinating an authorized sequence of tasks. These files do **not** install agents, register projects, grant permissions or start a service. The installed Runner and its [v2 protocol](../v2/operate-runner-v2/SKILL.md) remain the source of truth for command and result formats. Source availability is not evidence of live capability.

| Purpose / назначение | Skill / скилл | Russian / перевод |
| --- | --- | --- |
| Execute and inspect from ordinary ChatGPT / прямой запуск | [operate-runner-from-chat](chatgpt/operate-runner-from-chat/SKILL.md) | [Русский](chatgpt/operate-runner-from-chat/SKILL.ru.md) |
| Deliver a package to an enrolled local project / передача пакета | [deliver-handoff-to-runner](chatgpt/deliver-handoff-to-runner/SKILL.md) | [Русский](chatgpt/deliver-handoff-to-runner/SKILL.ru.md) |
| Integrate multi-step work via an existing Udot coordinator / координация через Udot | [execute-roadmap-via-chat-runner](udot/execute-roadmap-via-chat-runner/SKILL.md) | — |
| Register or prepare a project / регистрация, подготовка проекта | [prepare-runner-project-v2](../v2/prepare-runner-project-v2/SKILL.md) | [Русский](../v2/prepare-runner-project-v2/SKILL.ru.md) |
| Read/write the real command protocol / протокол команд | [operate-runner-v2](../v2/operate-runner-v2/SKILL.md) | [Русский](../v2/operate-runner-v2/SKILL.ru.md) |
| Install or upgrade Runner / установка или обновление | [install-runner-v2](../v2/install-runner-v2/SKILL.md) | [Русский](../v2/install-runner-v2/SKILL.ru.md) |

## Which one do I need? / Что выбрать?

**One-off ChatGPT task:** start with `operate-runner-from-chat` if you have an **already enrolled project and mailbox**. It refers to `operate-runner-v2` for the exact wire protocol. For a new project, use `prepare-runner-project-v2` and complete its [requested enrollment route](../../docs/PROJECT_ENROLLMENT.md#english); use the [task examples](../../examples/v2/README.md) when task preparation is also needed.

**Package handoff from ChatGPT:** use `deliver-handoff-to-runner` for one ZIP or several files addressed to a registered local `target_id`. It requires installed `DELIVER_HANDOFF` support, exact allowed Drive folder and command/result Docs tabs, real upload and independent metadata readback, then guarded DRAFT → READY through the existing operator flow. Runner 0.3.0.dev6 can resolve an omitted `drive_version` itself; older installations require an observed sender pin. It returns delivery evidence and a copyable Codex prompt; it does not execute the package. Check the selected installation and live tool capabilities before dispatch. Missing upload can use the explicitly labelled `MANUAL_UPLOAD` fallback; missing guarded Docs writing remains `TRANSPORT_BLOCKED`. Neither skill publication nor a manual fallback proves automatic end-to-end qualification.

**Udot:** `execute-roadmap-via-chat-runner` describes a **larger workflow** that uses a separate Udot coordinator, transit storage and shared workflow-state skills. Those dependencies are **not** distributed here. Reading the skill is not a complete Udot installation.

**Обычный ChatGPT:** сначала зарегистрируйте проект через `prepare-runner-project-v2`, завершив [нужный маршрут подключения](../../docs/PROJECT_ENROLLMENT.md#русский); при необходимости подготовьте задачи. Для конечной задачи используйте `operate-runner-from-chat`. Для Udot потребуются отдельный координатор и его общие инструкции.

**Передача пакета:** `deliver-handoff-to-runner` требует установленного действия, зарегистрированного локального target, разрешённой Drive folder и точных Docs tabs команды/результата. Runner 0.3.0.dev6 сам определяет опущенную `drive_version`; старым установкам нужна наблюдаемая версия отправителя. Upload, ACK, локальная доставка и чтение terminal receipt подтверждаются отдельно. После доставки владелец самостоятельно передаёт инструкцию Codex. Ручная загрузка помечается `MANUAL_UPLOAD`; без условной записи Docs отправка блокируется как `TRANSPORT_BLOCKED`.

## Provenance / происхождение

[PROVENANCE.json](PROVENANCE.json) preserves upstream source identities/blob SHAs and the handoff kit's original file SHA-256 values. The public copies adjust local links, clarify setup and replace inaccessible private-test links with published release evidence; later repository-local adaptations are recorded separately. The canonical library has not been edited or registered by these changes. The handoff kit is source provenance, not installed protocol authority. Check the **deployed** Runner version before applying version-specific instructions.

[Back to README](../../README.md#english) · [Вернуться к README](../../README.md#русский)
