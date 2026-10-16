---
name: ms-project
description: >-
  Read, analyze, edit, and regenerate Microsoft Project files (.mpp and MSPDI .xml) using
  MPXJ for extraction and Python for XML output. Use when the user needs to inspect
  schedules, tasks, dependencies, resources, calendars, baselines, or produce an updated
  project plan.
---


# Microsoft Project (.mpp) Tools

## Overview

Полный цикл работы с MS Project файлами: чтение, анализ, корректировка, генерация.

**Скрипты:**
- `scripts/mpp_read.py` — Чтение .mpp через MPXJ (Java)
- `scripts/mpp_write.py` — Генерация MSPDI XML на чистом Python
- `scripts/mpxj.jar` — MPXJ Java library (v13.10.0)
- `deps/` — Java-зависимости (POI, JAXB и др.)

Запускай команды из корня проекта, используя Python с установленным `jpype1`. Для исходного репозитория подготовь и активируй окружение:

```bash
uv sync --extra ms-project
source .venv/bin/activate
```

Пути `skills/ms-project/` ведут к публичному скиллу. Если он установлен отдельно, найди скрипты относительно каталога этого `SKILL.md`.

## Чтение .mpp (mpp_read.py)

### Базовое использование
```bash
python3 skills/ms-project/scripts/mpp_read.py project.mpp
```

### Форматы вывода
```bash
# JSON (default) — для обработки и генерации XML
python3 skills/ms-project/scripts/mpp_read.py project.mpp --format json > plan.json

# Markdown — для просмотра
python3 skills/ms-project/scripts/mpp_read.py project.mpp --format md

# CSV — для Excel
python3 skills/ms-project/scripts/mpp_read.py project.mpp --format csv
```

### Опции
```bash
# Только сводка
python3 skills/ms-project/scripts/mpp_read.py project.mpp --summary

# Конкретные поля
python3 skills/ms-project/scripts/mpp_read.py project.mpp --fields id,name,start,finish,percent_complete

# Фильтр по дате
python3 skills/ms-project/scripts/mpp_read.py project.mpp --filter-date 2026-04-01
```

### Доступные поля
`id, name, duration, start, finish, percent_complete, priority, resource_names, predecessors, notes, milestone, critical, cost, baseline_start, baseline_finish, actual_start, actual_finish, wbs, outline_level, constraint_type, constraint_date`

## Генерация XML (mpp_write.py)

### Базовое использование
```bash
python3 skills/ms-project/scripts/mpp_write.py plan.json --output project.xml
```

### С корректировками
```bash
python3 skills/ms-project/scripts/mpp_write.py plan.json \
  --output corrected.xml \
  --corrections corrections.json
```

### Формат corrections.json
```json
{
  "task_corrections": {
    "Имя задачи или ID": {
      "duration": 10,
      "start": "2026-05-01",
      "note": "+2д буфер"
    }
  },
  "project_notes": "Общие заметки к проекту"
}
```

## Полный workflow: Редактирование плана

### Шаг 1: Прочитать .mpp → JSON
```bash
python3 skills/ms-project/scripts/mpp_read.py project.mpp --format json > plan.json
```

### Шаг 2: Создать файл корректировок
```bash
cat > corrections.json << 'EOF'
{
  "task_corrections": {
    "Согласовать МКР": {"duration": 14, "note": "+4д буфер на согласование"},
    "Перенести в кубер": {"duration": 13, "note": "+3д на отладку"}
  },
  "project_notes": "План скорректирован: добавлены буферы на внешние зависимости"
}
EOF
```

### Шаг 3: Сгенерировать XML
```bash
python3 skills/ms-project/scripts/mpp_write.py plan.json \
  --output project_corrected.xml \
  --corrections corrections.json
```

### Шаг 4: Открыть в MS Project
- File → Open → выбрать `project_corrected.xml`
- Save as `.mpp`

## Структура JSON (формат mpp_read.py)

```json
{
  "summary": {
    "file_name": "",
    "start_date": "2026-04-01",
    "finish_date": "2026-07-30",
    "total_tasks": 34,
    "milestones": 0,
    "critical_tasks": 18,
    "resources_count": 11,
    "percent_complete": 21.4
  },
  "tasks": [
    {
      "id": 1,
      "name": "Задача",
      "duration": "10.0d",
      "start": "2026-04-01T09:00",
      "finish": "2026-04-14T18:00",
      "percent_complete": 50.0,
      "priority": 500,
      "predecessors": "[Task id=1 ...] -> [Task id=2 ...]",
      "notes": "",
      "milestone": false,
      "critical": true
    }
  ]
}
```

## Зависимости

### Системные
- Java (OpenJDK 21)
- Python 3.11+

### Python пакеты
```bash
uv pip install jpype1
# Или из корня исходного репозитория:
uv sync --extra ms-project
```

### Java библиотеки (каталог `deps/`)
- mpxj.jar (в scripts/)
- POI, JAXB и др. (в deps/)
- JAXB также требует `istack-commons-runtime-4.1.2.jar` и `txw2-4.0.5.jar`.
- Для .mpp нужны `rtfparserkit-1.16.0.jar` и `log4j-core-2.23.1.jar`.

## Ограничения

### Чтение (mpp_read.py)
- **Read-only для .mpp** — MPXJ не пишет в .mpp
- Некоторые кастомные поля могут быть недоступны
- Большие файлы (>10MB) — медленно из-за JVM

### Запись (mpp_write.py)
- Генерирует MSPDI XML (не .mpp напрямую)
- Ресурсы и назначения — базовая поддержка (можно расширить)
- Базовые линии (baselines) — не сохраняются из JSON

## Troubleshooting

### Ошибка: No module named 'jpype'
```bash
uv pip install jpype1
# Или из корня исходного репозитория:
uv sync --extra ms-project
```

### Ошибка: ClassNotFoundException (Java)
Проверить наличие deps/:
```bash
ls skills/ms-project/deps/
```

### Пересоздать deps при необходимости
Скрипт не скачивает зависимости. Восстанови отсутствующие JAR-файлы в `deps/` из Maven Central с указанными выше версиями:
```bash
cd skills/ms-project/deps/
# Скачать POI, JAXB jars (см. scripts/mpp_read.py)
```

## Расширение функциональности

mpp_write.py поддерживает расширение:
- Добавить поля в `generate_mspdi()`
- Расширить `corrections.json` схему
- Добавить поддержку ресурсов и назначений

См. исходный код скриптов для деталей.
