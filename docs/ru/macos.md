# Развёртывание на macOS

[English](../en/macos.md) | [Documentation index](../index.md)

Проект «из коробки» рассчитан на Linux (`scripts/install-systemd-user.sh`
+ `deploy/*.service.in`). На Mac те же расписания запускаются как
пользовательские LaunchAgents — по одному plist-у на сервис, все в
`~/Library/LaunchAgents/com.second-brain.*.plist`.

## Что нужно установить

| Утилита | Установка |
| --- | --- |
| Python 3.12 | `brew install python@3.12` (Apple Silicon или Intel) |
| `uv` | `brew install uv` или `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| `opencode` (или другой AI-CLI) | `brew install anomalyco/tap/opencode`, затем `opencode auth login` |
| `git` | `brew install git`, если ещё нет |

## Разовая настройка

```bash
# 1. Клон и зависимости
mkdir -p ~/second-brain ~/second-brain-data
git clone https://github.com/LKosoj/a-second-brain.git ~/second-brain/repo
cd ~/second-brain/repo
uv sync --frozen --no-dev

# 2. .env (init сам ставит 0600, но и руками не помешает)
cp .env.example .env
chmod 600 .env
$EDITOR .env
# Обязательно: TELEGRAM_BOT_TOKEN, DEEPGRAM_API_KEY, OWNER_TELEGRAM_ID
# Движок: AI_CLI=opencode (в .env.example по умолчанию claude)
# VAULT_PATH=$HOME/second-brain/repo/vault
# CONTENT_LANGUAGE=ru
# LIFE_SCOPE_DEFAULT=personal

# 3. Smoke-test
uv run --frozen --no-dev a-second-brain doctor ~/second-brain/repo
uv run --frozen --no-dev a-second-brain run
# Ctrl-C, как только бот напишет "Starting bot polling"

# 4. Установить LaunchAgents
bash scripts/install-launchd-user.sh --enable

# 5. Отключить сон, чтобы polling не прерывался
sudo pmset -a sleep 0 disksleep 0 displaysleep 0
```

`--enable` рендерит четыре plist-а из `deploy/*.plist.in`:

| Label | Расписание | Команда |
| --- | --- | --- |
| `com.second-brain.bot` | `RunAtLoad` + `KeepAlive` | `a-second-brain run` |
| `com.second-brain.process` | ежедневно в 21:00 | `python -m d_brain.run_daily_process --mode scheduled` |
| `com.second-brain.plaud-sync` | каждый час (только при `PLAUD_BEARER_TOKEN`) | `python -m d_brain.run_plaud_sync` |
| `com.second-brain.qmd-maintenance` | воскресенье 03:30 (только при `qmd` в `PATH`) | `a-second-brain qmd cleanup` |

## Управление

```bash
launchctl list | grep second-brain            # какие агенты загружены
launchctl start com.second-brain.bot     # принудительный запуск
launchctl stop com.second-brain.bot      # остановить бота
launchctl unload ~/Library/LaunchAgents/com.second-brain.bot.plist
launchctl load -w ~/Library/LaunchAgents/com.second-brain.bot.plist

# Логи (macOS не ротирует пользовательские логи — настройте logrotate сами)
ls ~/Library/Logs/com.second-brain/
tail -f ~/Library/Logs/com.second-brain/bot.err.log

# Запустить дневной цикл прямо сейчас (для отладки)
launchctl start com.second-brain.process
```

## Обновление

```bash
cd ~/second-brain/repo
git pull --ff-only
uv sync --frozen --no-dev
bash scripts/install-launchd-user.sh --enable
```

`--enable` идемпотентен: выгружает и загружает каждый агент заново с
новым содержимым plist-а.

## Удаление

```bash
bash scripts/install-launchd-user.sh --uninstall
sudo pmset -a sleep 1 disksleep 1 displaysleep 1   # вернуть дефолт
```

## Отличия от systemd

| systemd | launchd |
| --- | --- |
| `EnvironmentFile=@PROJECT_DIR@/.env` | `scripts/lib/run_with_env.sh` (soursit .env, запускается первым аргументом в `ProgramArguments`) |
| `Restart=on-failure` + `RestartSec=10` | `KeepAlive.Crashed: true` + `ThrottleInterval: 10` |
| `OnCalendar=*-*-* 21:00:00` | `StartCalendarInterval { Hour: 21, Minute: 0 }` |
| `OnCalendar=hourly` | `StartInterval: 3600` |
| `OnCalendar=Sun 03:30:00` | `StartCalendarInterval { Weekday: 0, Hour: 3, Minute: 30 }` |
| `journalctl -u a-second-brain` | `~/Library/Logs/com.second-brain/*.log` |

Plist-ы вызывают `run_with_env.sh`, чтобы подхватить `.env` так же, как
это делает `EnvironmentFile=` на Linux. Логи складываются в
`~/Library/Logs/` по конвенции Apple, а не в журнал systemd.