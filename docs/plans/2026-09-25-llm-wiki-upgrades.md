# Доработки по мотивам «LLM wiki» (Karpathy)

Источник идеи: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f

Цель: вики (`compiled/`) пополняется и чинится сама, каждый импорт доведён до
конца, ответы с числами приходят с графиками.

Правила для всех задач:

- vault — приватные данные; тесты строят временный vault (`tmp_path`,
  фикстура `write_vault_manifest` из `tests/conftest.py`).
- Запись в vault только через `write_validated_vault_markdown` /
  `patch_validated_vault_frontmatter` (резервные копии в `.session/ops-snapshots/`).
- Никаких коммитов, `git stash`, `git checkout`, `git reset`.
- Квалити-гейты (на этой машине `uv run` не работает):
  `PYTHONPATH=src:.venv/lib/python3.12/site-packages python3 -m pytest -q`,
  `.venv/lib/python3.12/site-packages/bin/ruff check src tests`,
  `PYTHONPATH=src:.venv/lib/python3.12/site-packages python3 -m mypy src`,
  `git diff --check`.
- Известное до начала работ падение, не относится к задаче:
  `tests/test_fix_cli_runner.py::test_processor_create_todoist_tasks_env_is_allowlisted`.

## Порядок и параллельность

Задачи делятся на волны так, чтобы два исполнителя не правили один файл
одновременно.

| Волна | Задачи | Общие файлы |
|-------|--------|-------------|
| 1 | T1 ∥ T3 | нет |
| 2 | T2 ∥ T6 | нет (T2 не трогает `processor.py` и `bot/`) |
| 3 | T4 | зависит от T2 (событие `compile`) |
| 4 | T5 | зависит от T2, T3, T4 |

Для каждой задачи: детальный план (сабагент) → реализация с тестами
(сабагент) → код-ревью (сабагент) → исправления, пока ревью не чистое.
После всех волн — общий цикл ревью всего диффа до нуля замечаний и финальная
ручная проверка.

## T1. Убрать кнопку «Сохранить»

Почему: кнопка дублирует автосохранение ответов
(`CompiledBriefingService.file_output_artifact` → `summaries/answers/` →
очередь compile-enrich), а её копия в `vault/answers/` в вики не попадает.

- `bot/handlers/do.py`, `bot/handlers/why.py`: убрать регистрацию ответа,
  клавиатуру `build_save_answer_keyboard`, обработчик `answer:save:*`.
- `services/answers.py`: удалить всё, что становится неиспользуемым (скорее
  всего модуль целиком, включая `append_log` — его заменит T2).
- Клавиатура, регистрация роутера, локализация, документы, упоминающие
  кнопку, — убрать упоминания.
- `tests/test_fix_answers.py`: удалить тесты удалённого кода.

Готово, когда: поиск `answer:save`, `register_pending_answer`,
`save_answer` по `src/ tests/ docs/` пуст; автосохранение не затронуто;
гейты зелёные.

## T2. Единый журнал операций

Новый модуль `services/ops_log.py`:

- `append_ops_log(vault_path, kind, summary, *, now=None) -> None`;
  файл `vault/.session/log.md`, строка `- ГГГГ-ММ-ДД ЧЧ:ММ [kind] summary`
  (формат прежнего `answers.append_log`), пробелы схлопываются в одну строку.
- `kind`: `ingest`, `compile`, `nightly`, `answer`, `fact-check`,
  `wiki-care`, `web-search`.
- Best-effort: `OSError` логируется и глотается, основную операцию не ломает.

Точки вызова:

- `ingest` — после записи заметки импорта: `web_archive.py`, `documents.py`,
  `youtube_transcript.py`, `plaud.py` (название + путь заметки).
- `compile` — в `compiled_briefings.py`, когда обработка одного источника
  из очереди завершилась: путь, число затронутых страниц или «ничего»/«ошибка».
- `nightly` — в конце `run_nightly_maintenance` (итоговые счётчики прохода).
- `answer` — после успешного `file_output_artifact`.
- `fact-check` — в конце `run_monthly_fact_check`.
- `wiki-care`, `web-search` — используются в T5.

T2 не трогает `processor.py` и `bot/`.

## T3. Каталог вики `MOC/compiled-index.md`

Новый модуль `services/compiled_index.py`:

- Читает `compiled/<domain>/*.md` (кроме `compiled/archive/`), frontmatter
  через существующий парсер.
- Строка: `- [[compiled/<domain>/<slug>|<Название>]] — <description> ·
  обновлено <updated> · источников <source_count> · <freshness_state>`.
  Название — первый `# H1`, иначе slug. Пустые поля пропускаются.
- Группы по доменам в фиксированном порядке (константа доменов из
  `compiled_briefings.py`), внутри — по алфавиту.
- Шапка: frontmatter (`type: note`, `description`, `tags`), пометка
  «генерируется автоматически, не править».
- В содержимом нет текущего времени — файл перезаписывается только при
  реальном изменении.
- Вызов в конце `run_nightly_maintenance` (best-effort) и отдельной
  CLI-командой не нужен.
- `skills/vault-retrieval/SKILL.md` (публичная копия в
  `src/d_brain/resources/project_template/skills/`): для обзорных вопросов
  сначала читать каталог.
- Ссылка на каталог в `MOC/index.md` шаблона vault (если он там есть) и
  один раз вручную в живом `vault/MOC/index.md` (запись на месте).

## T4. Импорты доводятся до конца

Пометка во frontmatter заметки импорта:

- `compile_state: used | nothing | failed`, `compile_checked: ГГГГ-ММ-ДД`.
- `used` — источник затронул ≥1 страницу вики или уже упомянут в ней;
  `nothing` — модель вернула «нечего брать» (пустой список обновлений);
  `failed` — источник выброшен из очереди после исчерпания попыток.

Где ставится:

- В очереди compile-enrich после обработки источника под `imports/`
  (там же, где T2 пишет событие `compile`).
- Ночной «сборщик хвостов» `sweep_unmarked_imports(limit)` в начале
  `run_nightly_maintenance`, до `drain_queue`:
  - заметка импорта без `compile_state`, на которую ссылается страница вики,
    → `used` без вызова модели;
  - уже в очереди → пропуск;
  - иначе, если старше суток, → `enqueue_refresh` (без debounce);
  - лимит постановок за ночь — 20.
- Заметки с `nothing` / `failed` повторно не ставятся.
- Первый проход по старым импортам — отдельная команда
  `run_compiled_import_sweep.py --no-limit`: без лимита постановок и без
  ночного бюджета вызовов модели. Запускает владелец.

Итог — счётчики в журнале прохода (`.session/compile-enrich.json`) и одна
строка в ночной сводке: «импорты: обработано N, пусто M, ошибок K».

## T5. Еженедельный уход за вики

Новый модуль `services/compiled_wiki_care.py`, workflow
`maintenance.compiled-wiki-care` (trigger `scheduled-post`, перед
`maintenance.compiled-digest`), метод `CliProcessor._run_compiled_wiki_care_cycle`,
CLI `run_compiled_wiki_care.py [--no-limit]`.

- Раз в неделю: журнал `.session/compile-wiki-care.json`, запуск, если
  прошло ≥7 дней.
- Бюджет — 10 вызовов модели за запуск; `--no-limit` снимает его (первый
  проход, запускает владелец).
- Действия (все меняют вики):
  1. Недостающие связи. Код выбирает пары: ≥2 общих источника без взаимной
     ссылки; название одной страницы в тексте другой без ссылки. Модель
     подтверждает пачками. Код пишет ссылки в обе страницы в раздел
     `## Related Pages`, которым владеет код (как `## Sources`), —
     перегенерация страницы ночным проходом раздел сохраняет.
  2. Недостающие страницы. Кандидаты — висячие ссылки на `compiled/...` и
     темы, которые модель находит по каталогу (T3). Страница создаётся тем же
     механизмом, что в ночном проходе (Verify, уровни доверия, настоящие
     источники).
  3. Пробелы из vault. Модель формулирует вопросы; бот отвечает по данным
     vault (`answer_question`), ответы идут через автосохранение в вики.
  4. Пробелы вне vault — веб-поиск. Модель без сети пишет запросы → код ищет
     через Tavily Search (ключ `TAVILY_API_KEY`) → модель выбирает ссылки из
     выдачи → код импортирует их обычным веб-импортом в `imports/web/auto/`
     → очередь compile-enrich. Лимит — 3 статьи за запуск (первый проход —
     10). Нет ключа — шаг пропускается. Уровень доверия `imports/web/auto/`
     — `forwarded`.
- Итог — блок в сводке со ссылками на изменения, события `wiki-care` и
  `web-search` в журнале T2.
- Регистрация: `registry.py`, `allowed_writes` родителя
  `maintenance.scheduled-cycle`, `docs/control-plane.md`, подписи в
  `processor.py`.

## T6. Графики в ответах

- Промпты ответа (`processor.py`, вопрос и `/do`): рисовать график, только если
  в ответе числа во времени или сравнение. Модель пишет код matplotlib сама:
  `matplotlib.use("Agg")`, шрифт DejaVu Sans, сохранение в
  `attachments/charts/ГГГГ-ММ-ДД-<slug>.png`, ссылка
  `![описание](attachments/charts/...)`.
- Доставка: если в ответе есть ссылки на картинки из `attachments/`, в чат
  уходит только HTML-файл с картинками внутри (base64 data URI), даже для
  короткого ответа; эти картинки не отправляются отдельными файлами.
- Картинки читаются только из `attachments/` внутри vault, с ограничением
  размера; отсутствующий файл — остаётся подпись.
- `matplotlib` — в зависимости проекта (`pyproject.toml`, `uv.lock`,
  установка в `.venv`).
