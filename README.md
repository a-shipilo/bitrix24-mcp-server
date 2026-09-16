# bitrix24-mcp-server

[![CI](https://github.com/a-shipilo/bitrix24-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/a-shipilo/bitrix24-mcp-server/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

MCP-сервер для работы Claude с **CRM, задачами, проектами, скрамом и бизнес-процессами Битрикс24** через входящий вебхук.
Запускается через `uvx` прямо из GitHub, устанавливать ничего не нужно.

Любое создание, изменение или удаление выполняется **только после подтверждения пользователем**.

*English: an open-source MCP server for Bitrix24 CRM (leads, deals, contacts, companies), tasks,
project kanban boards, Scrum sprints and business processes.
Every write operation requires explicit user approval. Run it with
`uvx --from git+https://github.com/a-shipilo/bitrix24-mcp-server bitrix24-mcp-server`.*

## Возможности

**CRM** — лиды, сделки, контакты, компании (универсальный API `crm.item.*`)

| Инструмент | Что делает |
|---|---|
| `crm_list` | поиск по фильтру с сортировкой и постраничным выводом |
| `crm_get` | карточка объекта со всеми заполненными полями, телефонами и e-mail |
| `crm_fields` | описание полей, включая пользовательские и варианты списков |
| `crm_stages` | воронки и стадии сделок, статусы лидов |
| `crm_find_by_contact_info` | поиск лидов, контактов и компаний по телефону или e-mail |
| `crm_comments` | комментарии из таймлайна |
| `crm_create` ✋ | создание |
| `crm_update` ✋ | изменение полей, например перевод сделки на другую стадию |
| `crm_delete` ✋ | удаление |
| `crm_add_comment` ✋ | комментарий в таймлайн |

**Задачи**

| Инструмент | Что делает |
|---|---|
| `tasks_list`, `task_get` | поиск задач и карточка задачи со списком вложений |
| `task_file_read` | содержимое вложения задачи (CSV, TSV, JSON, TXT) или сохранение файла на диск |
| `task_comments` | последние комментарии задачи: из чата задачи или из ленты комментариев |
| `task_create` ✋, `task_update` ✋ | создание и изменение, включая привязку к CRM |
| `task_complete` ✋, `task_delete` ✋ | завершение и удаление |
| `task_add_comment` ✋ | комментарий: в чат задачи, а на старых порталах — в ленту комментариев |
| `task_checklist`, `task_checklist_add` ✋, `task_checklist_complete` ✋ | чек-листы |
| `users_search`, `user_current` | поиск сотрудников, например чтобы узнать ID ответственного |

**Проекты и скрам**

| Инструмент | Что делает |
|---|---|
| `projects_list` | поиск проектов, рабочих групп и скрамов |
| `project_board` | канбан проекта: стадии и задачи на них |
| `task_move_stage` ✋ | перенос задачи на другую стадию канбана проекта |
| `scrum_sprints` | спринты скрама: активный, запланированные, завершённые |
| `sprint_board` | доска спринта: стадии, задачи, story points и эпики |
| `sprint_move_task` ✋ | перенос задачи на другую стадию спринта |
| `scrum_backlog` | бэклог в порядке приоритета со story points и эпиками |

**Бизнес-процессы**

| Инструмент | Что делает |
|---|---|
| `bp_templates`, `bp_template` | шаблоны из дизайнера: для каких документов, параметры запуска, дерево действий |
| `bp_start` ✋ | запуск бизнес-процесса для сделки, лида, контакта, компании или элемента списка |
| `bp_instances` | запущенные процессы, в том числе процессы роботов CRM и зависшие |
| `bp_terminate` ✋, `bp_kill` ✋ | остановка процесса; удаление процесса вместе с данными |
| `bp_tasks` | задания бизнес-процессов: утверждения, ознакомления, запросы информации |
| `bp_task_complete` ✋, `bp_task_delegate` ✋ | решение по заданию, передача задания другому сотруднику |

Создавать и менять шаблоны бизнес-процессов через входящий вебхук нельзя: Битрикс24 разрешает это
только приложениям. Методы бизнес-процессов доступны администратору портала.

✋ — операция выполняется только после подтверждения пользователем.

## Как работает подтверждение

Перед записью сервер показывает, что именно изменится:

```text
Изменение сделки #12 «Поставка оборудования»
Портал: https://example.bitrix24.ru
• stageId: NEW → WON
• opportunity: 150000 → 180000
```

Дальше всё зависит от клиента:

- **Claude Code** и другие клиенты с поддержкой [elicitation](https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation)
  показывают диалог подтверждения. Операция выполнится, только если нажать «Принять».
- **Claude Desktop** и клиенты без elicitation получают от инструмента не результат, а `confirmation_id`.
  Claude показывает вам описание операции и спрашивает разрешения.
  Операция выполнится только после вызова `confirm_action` с этим `confirmation_id`.
  Код подтверждения одноразовый и действует 15 минут.

> [!IMPORTANT]
> Когда Claude Desktop спросит разрешение на вызов `confirm_action`, не выбирайте «Always allow».
> Тогда Claude Desktop сам будет спрашивать вас перед каждой записью в Битрикс24,
> и ни одна запись не пройдёт без вашего клика.

## Установка

### 1. Создайте входящий вебхук в Битрикс24

1. Откройте **Приложения → Разработчикам → Другое → Входящий вебхук**.
2. Выдайте права:

   | Право | Для чего |
   |---|---|
   | **CRM** (`crm`) | лиды, сделки, контакты, компании |
   | **Задачи** (`task`) | задачи, чек-листы, канбан проектов, спринты и бэклог |
   | **Пользователи (минимальные)** (`user_brief`) | поиск сотрудников. Чтобы видеть их e-mail, выберите **Пользователи (базовые)** (`user_basic`) |
   | **Рабочие группы** (`sonet_group`) | список проектов и скрамов |
   | **Диск** (`disk`) | скачивание вложений задач (`task_file_read`) |
   | **Чат и уведомления** (`im`) | комментарии задач в новой карточке: они хранятся в чате задачи (`task_comments`) |
   | **Бизнес-процессы** (`bizproc`) | шаблоны, запуск и остановка процессов, задания |

   Права можно добавить позже: адрес вебхука при этом не меняется.

3. Скопируйте адрес вида `https://<ваш-портал>.bitrix24.ru/rest/1/xxxxxxxxxxxxxxxx/`.

> [!WARNING]
> Адрес вебхука — это пароль: с ним можно работать с CRM от имени вашего пользователя.
> Не публикуйте его и не коммитьте в репозитории.
> Сервер действует с правами пользователя, создавшего вебхук.

### 2. Подключите сервер в Claude Desktop

Нужен установленный [uv](https://docs.astral.sh/uv/getting-started/installation/)
(`brew install uv` на macOS).

1. Откройте **Settings → Developer → Edit Config**. Откроется файл `claude_desktop_config.json`.
2. Добавьте сервер в `mcpServers`:

```json
{
  "mcpServers": {
    "bitrix24": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/a-shipilo/bitrix24-mcp-server@v0.2.2",
        "bitrix24-mcp-server"
      ],
      "env": {
        "BITRIX24_WEBHOOK_URL": "https://your-portal.bitrix24.ru/rest/1/xxxxxxxxxxxxxxxx/"
      }
    }
  }
}
```

3. Полностью перезапустите Claude Desktop. Сервер `bitrix24` появится в **Settings → Developer**
   со статусом *running*, а его инструменты — в меню подключений в чате.

`@v0.2.2` фиксирует версию. Чтобы всегда брать последнюю версию из `main`, уберите `@v0.2.2`.
Для обновления добавьте в `args` перед `--from` флаг `--refresh`.

Если в логах `spawn uvx ENOENT`, укажите полный путь к uvx (узнать его: `which uvx`),
например `"command": "/opt/homebrew/bin/uvx"`.

### Claude Code

```bash
claude mcp add bitrix24 -e BITRIX24_WEBHOOK_URL=https://your-portal.bitrix24.ru/rest/1/xxxxxxxxxxxxxxxx/ -- uvx --from git+https://github.com/a-shipilo/bitrix24-mcp-server bitrix24-mcp-server
```

## Настройки

| Переменная | По умолчанию | Описание |
|---|---|---|
| `BITRIX24_WEBHOOK_URL` | — | адрес входящего вебхука, обязательно |
| `BITRIX24_CONFIRM_MODE` | `auto` | `auto` — диалог, если клиент его поддерживает, иначе `confirmation_id`; `elicitation` — только диалог; `token` — всегда `confirmation_id` |
| `BITRIX24_CONFIRM_TASKS` | `true` | `false` — все операции с задачами и проектами без подтверждения |
| `BITRIX24_AUTO_APPROVE` | — | инструменты, которые выполняются без подтверждения, через запятую, например `task_add_comment,task_move_stage` |

Для CRM подтверждение отключить нельзя: `crm_*` в `BITRIX24_AUTO_APPROVE` игнорируются.

## Автономные сценарии

В рутине, которая работает без человека, подтверждать запись некому. Разрешите нужные инструменты
через `BITRIX24_AUTO_APPROVE`. Удобно завести для рутины отдельное подключение: в интерактивной
работе подтверждения останутся.

```json
"bitrix24-routine": {
  "command": "uvx",
  "args": ["--from", "git+https://github.com/a-shipilo/bitrix24-mcp-server@v0.2.2", "bitrix24-mcp-server"],
  "env": {
    "BITRIX24_WEBHOOK_URL": "https://your-portal.bitrix24.ru/rest/1/xxxxxxxxxxxxxxxx/",
    "BITRIX24_AUTO_APPROVE": "task_add_comment,task_move_stage,sprint_move_task"
  }
}
```

Инструмент без подтверждения сразу возвращает `status: done` и в поле `operation` — описание того,
что было сделано. Его удобно сохранять в журнал рутины.

## Примеры запросов

- «Покажи мои сделки на стадии "Переговоры" дороже 100 000»
- «Найди контакт с телефоном +7 999 123-45-67 и покажи его сделки»
- «Переведи сделку 1542 в "Успешно" и оставь комментарий "Договор подписан"»
- «Создай задачу Анне Смирновой подготовить КП по сделке 1542 до пятницы»
- «Какие мои задачи просрочены?»
- «Покажи доску текущего спринта в скраме "Мобильное приложение" и сколько story points осталось»
- «Что сейчас в работе в проекте "Переезд офиса"? Перенеси задачу 318 в "Готово"»

## Разработка

```bash
git clone https://github.com/a-shipilo/bitrix24-mcp-server.git
cd bitrix24-mcp-server
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Локальная отладка в [MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
npx @modelcontextprotocol/inspector -e BITRIX24_WEBHOOK_URL=https://... uv run bitrix24-mcp-server
```

## Лицензия

[MIT](LICENSE)
