---
type: note
last_accessed: 2026-04-25
relevance: 0.32
tier: archive
---
# Sequential Thinking MCP Skill

> MCP сервер для структурированного мышления и пошагового решения сложных проблем.
> Установлен через: `npx -y @modelcontextprotocol/server-sequential-thinking`
> Статус: ✅ Активен (через mcporter)

---

## Установка

```bash
# Через mcporter (уже установлено)
mcporter config add sequential-thinking --command "npx -y @modelcontextprotocol/server-sequential-thinking"

# Проверка
mcporter list
```

---

## Инструмент

### `sequentialthinking`

Детальный инструмент для динамического и рефлексивного решения проблем через последовательность мыслей.

**Параметры:**

| Параметр | Тип | Обязательный | Описание |
|----------|-----|--------------|----------|
| `thought` | string | ✅ | Текущий шаг мышления |
| `nextThoughtNeeded` | boolean | ✅ | Нужен ли ещё один шаг |
| `thoughtNumber` | int | ✅ | Номер текущей мысли (1, 2, 3...) |
| `totalThoughts` | int | ✅ | Оценка общего количества шагов |
| `isRevision` | boolean | ❌ | Пересматривает ли эта мысль предыдущую |
| `revisesThought` | int | ❌ | Какую мысль пересматриваем |
| `branchFromThought` | int | ❌ | Точка ветвления |
| `branchId` | string | ❌ | Идентификатор ветки |
| `needsMoreThoughts` | boolean | ❌ | Нужно ли больше шагов |

**Возможности:**
- Разбивка сложных проблем на шаги
- Пересмотр и уточнение мыслей по ходу анализа
- Ветвление на альтернативные пути рассуждений
- Динамическая корректировка количества шагов
- Генерация и верификация гипотез

---

## Когда использовать

- Разбор сложных проблем по шагам
- Планирование с возможностью пересмотра
- Анализ, который может потребовать корректировки курса
- Задачи, где полный объём не ясен изначально
- Многошаговые решения
- Поддержание контекста на протяжении нескольких шагов
- Фильтрация нерелевантной информации

---

## Примеры вызова

### Базовый пример

```bash
mcporter call sequential-thinking.sequentialthinking \
  thought="Начинаю анализ проблемы производительности" \
  thoughtNumber=1 \
  totalThoughts=5 \
  nextThoughtNeeded=true
```

### С пересмотром

```bash
mcporter call sequential-thinking.sequentialthinking \
  thought="Уточняю предыдущую мысль: проблема не в БД, а в кеше" \
  thoughtNumber=3 \
  totalThoughts=5 \
  nextThoughtNeeded=true \
  isRevision=true \
  revisesThought=2
```

### С ветвлением

```bash
mcporter call sequential-thinking.sequentialthinking \
  thought="Альтернативный подход: использовать очередь вместо прямых вызовов" \
  thoughtNumber=4 \
  totalThoughts=6 \
  nextThoughtNeeded=true \
  branchFromThought=3 \
  branchId="alternative-approach"
```

---

## Интеграция с персонами

| Персона | Как использует |
|---------|---------------|
| **analyst** | Для сложной аналитики, многошагового анализа, сравнения альтернатив |
| **critic** | Для системной критики идей, поиска слабых мест через последовательное рассуждение |
| **researcher** | Для структурирования сложных исследований, разбора проблем по этапам |
| **product-manager** | Для анализа trade-off'ов, оценки рисков фич |
| **sysadmin** | Для пошаговой диагностики проблем, root cause analysis |

---

## Примеры задач для персон

### Analyst
```
"Проанализируй риски миграции базы данных" → sequential-thinking
"Сравни 3 архитектурных решения" → sequential-thinking с ветвлением
```

### Critic
```
"Разбери слабые места бизнес-модели" → sequential-thinking
"Найди 5 сценариев провала проекта" → sequential-thinking с ветвлением
```

### Researcher
```
"Исследуй рынок AI-видео" → sequential-thinking + web-research
"Разбери причины сбоя системы" → sequential-thinking с пересмотром
```

### Product Manager
```
"Оцени trade-off между скоростью и надёжностью" → sequential-thinking
"Проанализируй риски запуска MVP" → sequential-thinking
```

### Sysadmin
```
"Диагностируй почему сервер падает каждые 2 часа" → sequential-thinking
"Найди root cause медленных запросов" → sequential-thinking с ветвлением
```

---

*Установлен: 2026-04-25*
*Версия: latest (npx)*
