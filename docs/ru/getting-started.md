# Начало работы

[English](../en/getting-started.md) | [Оглавление документации](../index.md)

Это руководство поможет установить A Second Brain так, чтобы личные данные из
vault не попали в публичный репозиторий с кодом.

## Требования

- Linux и Python 3.12 или новее;
- `uv` и `jq`;
- токен Telegram-бота от BotFather;
- API-ключ Deepgram для распознавания голосовых сообщений;
- один поддерживаемый AI CLI: Claude Code, Codex CLI, Qwen Code, Gemini CLI
  или Kimi Code;
- непривилегированная учётная запись Linux.

Для дополнительных интеграций могут потребоваться другие программы и ключи.
Подробнее — в разделе [Интеграции](integrations.md).

## Установка из исходного кода

Клонируйте будущий публичный репозиторий в каталог, принадлежащий учётной
записи сервиса:

```bash
git clone <PUBLIC_REPOSITORY_URL> a-second-brain
cd a-second-brain
./install.sh
```

Установщик:

1. откажется работать от имени root;
2. установит зафиксированные runtime-зависимости без группы разработки;
3. создаст приватный vault, только если `vault/` ещё не существует;
4. создаст `.env` с правами `0600`.

Он не запускает сервисы и не публикует данные.

## Установка из wheel

Скачайте wheel из release проекта либо соберите его из исходного checkout
командой `uv build`. Установите wheel в Python-окружение, затем создайте
приватный экземпляр:

```bash
python -m pip install a_second_brain-0.1.0-py3-none-any.whl
a-second-brain init /srv/a-second-brain-instance
cd /srv/a-second-brain-instance
```

Команда создаёт `.env`, `.env.example`, `.gitignore`, `mcp-config.json`,
`vault-manifest.json` и обезличенный стартовый `vault/`. Существующий vault
она не перезаписывает.

## Минимальная конфигурация

Откройте `.env` и заполните три обязательных значения:

```dotenv
TELEGRAM_BOT_TOKEN=
DEEPGRAM_API_KEY=
OWNER_TELEGRAM_ID=
```

`OWNER_TELEGRAM_ID` — единственная учётная запись Telegram, которой разрешён
доступ к боту. Укажите положительный числовой Telegram user ID.

По умолчанию `AI_CLI` равен `claude`. Задавайте его только для выбора другого
поддерживаемого backend:

```dotenv
AI_CLI=codex
```

Отдельно авторизуйте выбранный AI CLI. Например:

```bash
codex login
```

Все параметры описаны в разделе [Конфигурация](configuration.md).

## Проверка установки

В исходном checkout запустите встроенную диагностику через зафиксированное
окружение:

```bash
uv run --frozen --no-dev a-second-brain doctor
```

Если пакет установлен в активном Python-окружении, вызывайте исполняемую
команду напрямую:

```bash
a-second-brain doctor
```

Если команда выполняется из другого каталога, явно укажите экземпляр:

```bash
a-second-brain doctor /srv/a-second-brain-instance
```

Флаг `--smoke` выполнит один короткий запрос через выбранный AI CLI:

```bash
a-second-brain doctor --smoke
```

До запуска бота устраните все сообщения `ERR`. Предупреждения `WARN` не меняют
код завершения, но их нужно проверить.

## Первый запуск в foreground

Из исходного checkout:

```bash
uv run --frozen --no-dev a-second-brain run
```

Из активного окружения с установленным wheel:

```bash
a-second-brain run
```

Отправьте боту `/menu` из учётной записи владельца. Запросы от других
пользователей Telegram отклоняются.

## Установка пользовательских сервисов

Поставляемые шаблоны systemd и установщик относятся к исходному checkout; в
созданный из wheel экземпляр они не входят. После успешного foreground-запуска
из checkout:

```bash
./scripts/install-systemd-user.sh
./scripts/install-systemd-user.sh --enable
```

Первая команда только создаёт systemd user units. Вторая запускает `doctor`,
затем включает бота и обработку по расписанию. Дальнейшая эксплуатация описана
в разделе [Эксплуатация](operations.md).

При установке только из wheel продолжайте использовать foreground-режим либо
создайте сервис на уровне хоста. Он должен вызывать абсолютный путь к
`a-second-brain` из нужного Python-окружения и использовать каталог экземпляра
как рабочий.
