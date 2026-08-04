# Справочник CLI

[English](../en/cli.md) | [Оглавление документации](../index.md)

Пакет устанавливает одну исполняемую команду:

```text
a-second-brain
```

## `a-second-brain init`

Создание приватного экземпляра:

```bash
a-second-brain init [PROJECT_DIR]
```

Если `PROJECT_DIR` не указан, используется текущий каталог. Команда создаёт
обезличенный vault и локальную конфигурацию. Существующие файлы проекта
сохраняются, а при наличии `vault/` команда завершается с кодом `2`, не
перезаписывая данные.

Пример:

```bash
a-second-brain init /srv/a-second-brain-instance
```

Создаются:

- `.env` с правами `0600`;
- `.env.example`;
- `.gitignore`, защищающий приватные runtime-данные;
- `mcp-config.json`;
- `vault-manifest.json`;
- `vault/` с универсальными промптами, политиками и стартовыми заметками.

## `a-second-brain doctor`

Проверка конфигурации и зависимостей:

```bash
a-second-brain doctor [PROJECT_DIR] [--smoke]
```

Команда проверяет:

- наличие и права `.env`;
- обязательные переменные без вывода их значений;
- корректность ID владельца;
- vault, manifest и MCP-конфигурацию;
- `uv`, `jq`, выбранный AI CLI и его авторизацию;
- условные требования Todoist и зашифрованных резервных копий;
- состояние необязательных QMD и PLAUD.

Флаг `--smoke` отправляет через выбранный AI CLI запрос
`Reply with exactly OK`. Содержимое vault при этом не передаётся.

Уровни сообщений:

| Уровень | Значение |
|---|---|
| `OK` | Проверка пройдена |
| `INFO` | Необязательная функция не настроена |
| `WARN` | Работу можно продолжить, но результат нужно проверить |
| `ERR` | Обязательная настройка или зависимость не прошла проверку |

Коды завершения:

| Код | Значение |
|---:|---|
| `0` | Ошибок нет; предупреждения допустимы |
| `1` | Одна или несколько проверок завершились ошибкой |
| `2` | Некорректные аргументы CLI либо `init` обнаружил существующий vault |

Старый путь `./scripts/doctor.sh` сохранён для исходных checkout как тонкая
обёртка над этой командой.

## `a-second-brain qmd`

Запуск QMD для приватного индекса vault:

```bash
a-second-brain qmd [АРГУМЕНТЫ_QMD...]
```

Примеры:

```bash
a-second-brain qmd status
a-second-brain qmd update
a-second-brain qmd recall "фокус недели"
```

Без аргументов команда показывает состояние индекса.

## `a-second-brain run`

Запуск Telegram-бота в foreground:

```bash
a-second-brain run
```

В исходном checkout с lock-файлом предпочтительна команда:

```bash
uv run --frozen --no-dev a-second-brain run
```

Остановите foreground-процесс сочетанием `Ctrl+C`. В production используйте
предоставленный systemd user unit.

## Общая справка

```bash
a-second-brain --help
a-second-brain init --help
a-second-brain doctor --help
a-second-brain qmd --help
a-second-brain run --help
```
