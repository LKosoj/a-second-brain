# Интеграции

[English](../en/integrations.md) | [Оглавление документации](../index.md)

Интеграции включаются оператором через конфигурацию. Храните учётные данные в
`.env`, изучайте условия хранения данных каждого провайдера и выдавайте только
необходимые разрешения.

## AI CLI

Выберите один backend:

```dotenv
AI_CLI=claude
```

Поддерживаемые значения и типовые команды авторизации:

| Backend | Авторизация |
|---|---|
| Claude Code | `claude auth login` |
| Codex CLI | `codex login` |
| Qwen Code | `qwen auth qwen-oauth` |
| Gemini CLI | настройте поддерживаемые учётные данные Google/Gemini |
| Kimi Code | `kimi login` |

Проверка установки и авторизации:

```bash
a-second-brain doctor --smoke
```

Для выполнения workflow агентский CLI получает выбранный контекст из vault.
Считайте поставщика CLI и настроенный model gateway обработчиками приватных
данных.

Kimi Code запускается в официальном режиме ACP (`kimi acp`). ACP — это
протокол обмена запросами через ввод и вывод процесса, поэтому текст запроса
не попадает в аргументы команды. Общие скиллы Kimi находит через
`.agents/skills`; отдельная копия для Kimi не нужна.

## Deepgram

Deepgram распознаёт голосовые сообщения Telegram. Настройте:

```dotenv
DEEPGRAM_API_KEY=
```

Текущему runtime этот ключ необходим. Аудио отправляется в настроенный сервис
Deepgram для транскрипции.

## Todoist

Для операций с Todoist нужны:

- `TODOIST_API_KEY`;
- `mcp-cli`;
- `npx`;
- локальный `mcp-config.json`.

В конфигурации зафиксирована версия Todoist MCP-пакета. Создавать задачи могут
только workflow, которым явно разрешена эта операция и в которых определена
ответственность владельца.

Чтобы отключить Todoist, оставьте `TODOIST_API_KEY` пустым. В этом случае
`doctor` выведет информационное сообщение, а не ошибку.

## QMD

[QMD](https://github.com/tobi/qmd) обеспечивает локальную индексацию и поиск по
vault. Проект проверен с `@tobilu/qmd` 2.5.3. Установите Node.js, затем
попросите администратора установить зафиксированный CLI в системный prefix:

```bash
sudo npm install -g @tobilu/qmd@2.5.3
qmd --version
```

Стандартный режим полностью локальный и не требует OpenAI-совместимых
переменных ниже. Если `EMB_MODEL` пуст, проект использует
`Qwen3-Embedding-0.6B`; если `RERANK_MODEL` пуст, QMD использует
`Qwen3-Reranker-0.6B`. Модели загружаются с Hugging Face при первой индексации
или первом запросе и по умолчанию работают на CPU. Для удалённого провайдера
эмбеддингов задайте `OPENAI_API_KEY` и `EMB_MODEL`; для нестандартного
совместимого endpoint также нужен `BASE_URL`. `MODEL` относится к планировщику
recall, а не к индексации QMD.

```dotenv
OPENAI_API_KEY=
BASE_URL=
MODEL=
EMB_MODEL=
RERANK_MODEL=
```

Создайте или пересоберите локальный индекс проекта из исходного checkout:

```bash
uv run --frozen --no-dev a-second-brain qmd update
uv run --frozen --no-dev a-second-brain qmd embed
uv run --frozen --no-dev a-second-brain qmd status
```

Из активного окружения с установленным wheel:

```bash
a-second-brain qmd update
a-second-brain qmd embed
a-second-brain qmd status
```

Индекс QMD — производные данные, а не источник истины. Каноническими остаются
Markdown-файлы vault. Еженедельная очистка включается, только если `qmd`
доступен во время установки systemd units. Если QMD позднее удалён, отключите
его таймер:

```bash
systemctl --user disable --now a-second-brain-qmd-maintenance.timer
```

## PLAUD

Для импорта записей задайте:

```dotenv
PLAUD_REGION=api
PLAUD_BEARER_TOKEN=
```

Поддерживаются регионы `api` и `api-euc1`. Необязательный ежечасный таймер
включается только при наличии токена.

Удаление токена не отключает ранее включённый таймер:

```bash
systemctl --user disable --now a-second-brain-plaud-sync.timer
```

Импортированные расшифровки могут содержать личные и деловые сведения. Они
хранятся в приватном vault и не должны попадать в публичный репозиторий с
кодом.

## Извлечение веб-страниц

Необязательные сервисы извлечения настраиваются через `TAVILY_API_KEY`,
`JINA_API_KEY`, `ZAI_API_KEY` и `PROXY_URL`. Runtime может сохранять исходный
материал и извлечённый текст в области импорта внутри vault.

## Зашифрованные резервные копии

GPG — внешняя интеграция. На сервере приложения должен находиться только
открытый ключ получателя. Подробнее — в разделе
[Резервное копирование и восстановление](backup-and-restore.md).
