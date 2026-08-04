# A Second Brain

[English](README.md)

Самостоятельно размещаемый голосовой персональный ассистент в Telegram. Он
сохраняет текст и пересланные сообщения, голосовые сообщения, фотографии,
документы, веб- и YouTube-ссылки в приватный Obsidian-совместимый vault.
Ассистент может импортировать расшифровки и сводки из необязательных записей
PLAUD, отвечать по содержимому vault и создавать задачи в Todoist.

[Полная документация](docs/index.md) описывает установку, конфигурацию,
эксплуатацию, интеграции, резервное копирование, устранение неполадок,
архитектуру и разработку на русском и английском языках.

В репозитории находятся код и обезличенный шаблон стартового vault. Настоящие
`vault/`, `.env`, резервные копии, логи и runtime-данные игнорируются Git и
должны оставаться приватными.

## Требования

- отдельная непривилегированная учётная запись Linux с Python 3.12 или новее;
- [uv](https://docs.astral.sh/uv/);
- `jq`;
- токен Telegram-бота от BotFather;
- API-ключ Deepgram для распознавания голоса;
- один установленный и авторизованный AI CLI: Claude Code, Codex CLI,
  Qwen Code, Gemini CLI или Kimi Code.

Todoist, веб-экстракция, PLAUD, QMD и зашифрованные резервные копии
необязательны и требуют дополнительных зависимостей и настроек. См. разделы
[Интеграции](docs/ru/integrations.md) и
[Конфигурация](docs/ru/configuration.md).

## Установка из клона

Пока публичный remote не создан, канонического адреса для клонирования нет.
Этот фрагмент запросит HTTPS- или SSH-адрес, который показывает Git-хост:

```bash
read -r -p "Repository URL: " REPOSITORY_URL
git clone "$REPOSITORY_URL" a-second-brain
cd a-second-brain
./install.sh
```

Заполните в `.env` `TELEGRAM_BOT_TOKEN`, `DEEPGRAM_API_KEY` и
`OWNER_TELEGRAM_ID`. Задавать `AI_CLI` нужно только при выборе backend,
отличного от стандартного `claude`. Бот принимает запросы только от
Telegram-аккаунта с указанным `OWNER_TELEGRAM_ID`. Затем:

```bash
uv run --frozen --no-dev a-second-brain doctor
uv run --frozen --no-dev a-second-brain run
```

`install.sh` не запускается от root, устанавливает зависимости строго по
lock-файлу и создаёт приватный vault, только если его ещё нет.

## Установка из wheel

Скачайте wheel со страницы Releases на Git-хосте либо соберите его в исходном
checkout командой `uv build`. Поместите один wheel в текущий каталог. В
активированном окружении Python 3.12+ с `pip` выполните:

```bash
python -m pip install ./a_second_brain-*.whl
INSTANCE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/a-second-brain"
a-second-brain init "$INSTANCE_DIR"
cd "$INSTANCE_DIR"
```

`init` создаёт универсальный vault, `.env.example`, закрытый правами `0600`
файл `.env`, `mcp-config.json`, `vault-manifest.json` и защитный `.gitignore`.
Существующие файлы проекта сохраняются, а существующий vault не
перезаписывается.

Заполните `.env`, как описано выше, затем выполните:

```bash
a-second-brain doctor
a-second-brain run
```

## Пользовательский сервис из исходного checkout

Штатный установщик systemd входит только в исходный checkout и отсутствует в
экземпляре, созданном из wheel. Для него нужен работающий systemd user manager.

Сначала сгенерируйте systemd user units без включения:

```bash
./scripts/install-systemd-user.sh
```

Проверьте файлы в
`${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/`, затем включите бота и
расписание:

```bash
./scripts/install-systemd-user.sh --enable
```

При запуске с `--enable` установщик сначала выполняет `doctor`. Если диагностика
успешна, он включает и запускает бота и ежедневную обработку. Таймер PLAUD
включается и запускается при непустом `PLAUD_BEARER_TOKEN`, а таймер QMD —
когда команда `qmd` доступна в `PATH`. Последующее удаление любого из этих
условий не отключает уже включённый таймер; см. раздел
[Интеграции](docs/ru/integrations.md). Системный root-сервис установщик не
создаёт.

## Граница приватности

- В исходном checkout созданный `vault/` находится внутри каталога проекта,
  но игнорируется Git; он должен оставаться приватным.
- Никогда не переносите сюда заполненный vault, `.env`, логи, резервные копии
  или runtime-данные.
- Считайте `.gitignore` защитой от случайной публикации, а не контролем доступа
  или шифрованием; ограничьте доступ к файлам учётной записью сервиса.
- Самостоятельное размещение не делает всю обработку локальной. Telegram
  передаёт сообщения и загруженные файлы бота, а голосовые сообщения
  отправляются в Deepgram. Настроенный AI CLI работает с доступом к приватному
  vault и может передавать его контекст поставщику модели. Todoist обменивается
  данными задач и проектов; веб-экстракторы получают URL и содержимое страниц;
  YouTube получает запросы на получение материалов; синхронизация PLAUD
  запрашивает метаданные и расшифровки записей; удалённый QMD отправляет
  фрагменты заметок настроенному endpoint для эмбеддингов или reranking.
- Перед каждым публичным релизом запускайте сканер секретов.
- Если ключ когда-либо попал в коммит, его нужно отозвать; удалить его
  последующим коммитом недостаточно.

Подробнее — в [SECURITY.md](SECURITY.md).

## Разработка

Полный список обязательных проверок приведён в разделе
[Разработка](docs/ru/development.md#проверки-качества).

```bash
uv sync --frozen --group dev
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen pytest -q
uv build
```

Контракты workflow и маршрутизация находятся в
`src/d_brain/control_plane/registry.py` и
`src/d_brain/control_plane/router.py`. Runtime-оркестрация реализована в
`src/d_brain/services/`, включая `daily_workflow.py`; встроенный vault содержит
runtime-промпты, политики и стартовые материалы. См.
[ARCHITECTURE.md](ARCHITECTURE.md) и
[docs/control-plane.md](docs/control-plane.md).

## Лицензия

MIT. См. [LICENSE](LICENSE) и [NOTICE](NOTICE).
