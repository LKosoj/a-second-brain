# Эксплуатация

[English](../en/operations.md) | [Оглавление документации](../index.md)

Запускайте A Second Brain от отдельной непривилегированной учётной записи
Linux. Штатная установка использует systemd user units и не создаёт сервис от
имени root. Команды на этой странице предполагают исходный checkout и рабочий
systemd user manager. В экземпляре, созданном только из wheel, штатного
установщика units нет.

## Проверка в foreground

Перед включением сервисов:

```bash
cd /path/to/a-second-brain
uv run --frozen --no-dev a-second-brain doctor --smoke
uv run --frozen --no-dev a-second-brain run
```

Убедитесь, что `/menu` работает из Telegram-аккаунта владельца, затем
остановите foreground-процесс.

## Установка systemd user units

Создайте units, не включая их:

```bash
./scripts/install-systemd-user.sh
```

Проверьте файлы в `~/.config/systemd/user/`. После проверки включите штатные
units:

```bash
./scripts/install-systemd-user.sh --enable
```

Перед включением установщик запускает `doctor`.

## Поставляемые units

| Unit | Назначение |
|---|---|
| `a-second-brain.service` | Telegram-бот |
| `a-second-brain-process.timer` | Ежедневная обработка в 21:00 |
| `a-second-brain-plaud-sync.timer` | Ежечасная синхронизация PLAUD, если она настроена |
| `a-second-brain-qmd-maintenance.timer` | Еженедельная очистка QMD при наличии `qmd` |

Перед ежедневной обработкой с записью запускается предварительное
зашифрованное резервное копирование. Если GPG-получатель не задан, этот этап
сообщает о пропуске и успешно завершается.

## Управление сервисом

```bash
systemctl --user status a-second-brain.service
systemctl --user restart a-second-brain.service
systemctl --user stop a-second-brain.service
systemctl --user start a-second-brain.service
```

Проверка таймеров:

```bash
systemctl --user list-timers 'a-second-brain-*'
systemctl --user status a-second-brain-process.timer
```

## Логи

Приложение пишет в stdout и stderr. При запуске через systemd используйте
journal:

```bash
journalctl --user -u a-second-brain.service -n 200 --no-pager
journalctl --user -u a-second-brain.service -f
journalctl --user -u a-second-brain-process.service --since today
```

Перед публикацией логов проверьте, нет ли в них записанного текста, путей,
Telegram ID и ошибок провайдеров с чувствительными данными.

## Обновление исходного checkout

Перед значительным обновлением остановите процессы, которые могут писать
данные:

```bash
for unit in \
  a-second-brain-process.timer \
  a-second-brain-plaud-sync.timer \
  a-second-brain-qmd-maintenance.timer \
  a-second-brain.service \
  a-second-brain-process.service \
  a-second-brain-plaud-sync.service \
  a-second-brain-qmd-maintenance.service
do
  systemctl --user stop "$unit" 2>/dev/null || true
done
git pull --ff-only
uv sync --frozen --no-dev
uv run --frozen --no-dev a-second-brain doctor
./scripts/install-systemd-user.sh --enable
```

Если изменились deployment-шаблоны, установщик выполнит `daemon-reload`.
Перед миграциями изучите release notes и создайте проверенную резервную копию.

## Ручной запуск обслуживания

```bash
uv run --frozen --no-dev python -m d_brain.run_daily_process --mode scheduled
uv run --frozen --no-dev python -m d_brain.run_plaud_sync
uv run --frozen --no-dev a-second-brain qmd cleanup
uv run --frozen --no-dev python -m d_brain.run_vault_backup
```

Ручные команды используют тот же `.env` и рабочий каталог, что и сервисы.

## Работа после выхода из системы

Обычно пользовательские сервисы останавливаются, когда у учётной записи нет
активной сессии. Администратор может включить lingering для выделенной учётной
записи:

```bash
sudo loginctl enable-linger <service-account>
```

Это решение уровня хоста, поэтому установщик проекта намеренно его не
принимает автоматически.
