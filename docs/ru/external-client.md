# Подключение внешнего клиента

[English](../en/external-client.md) | [Оглавление документации](../index.md)

Внешний клиент может отправлять запросы в A Second Brain по закрытому HTTP API.
Клиент передаёт обычный текст, а сервер сам определяет, что делать:

- ответить на вопрос;
- сохранить запись в daily;
- выполнить поручение на сервере.

Текущий request API не предоставляет произвольный доступ к файлам vault. Для
работы с файлами сейчас используйте отдельный механизм удалённой папки или
синхронизации.

## 1. Настройте сервер

В `.env` проекта задайте токен и адрес API:

```dotenv
NETWORK_API_TOKEN=длинный-случайный-токен
NETWORK_API_HOST=127.0.0.1
NETWORK_API_PORT=8765
```

Токен должен быть случайным и храниться только в `.env`. После изменения
настроек перегенерируйте systemd units и запустите сервис:

```bash
./scripts/install-systemd-user.sh --enable
```

Общий скрипт перезапуска также проверяет `NETWORK_API_TOKEN`: при непустом
значении он запускает сетевой сервис, а при пустом пропускает этот шаг.

Проверьте состояние:

```bash
systemctl --user is-active a-second-brain-network.service
curl http://127.0.0.1:8765/healthz
```

Ожидаемый ответ health-check:

```json
{"status": "ok"}
```

## 2. Подключитесь через SSH-туннель

По умолчанию API слушает только `127.0.0.1`, поэтому его порт не нужно
открывать в интернет. На компьютере с внешним клиентом запустите:

```bash
ssh -N -L 8765:127.0.0.1:8765 <user>@<server>
```

После этого внешний клиент обращается к `http://127.0.0.1:8765` на своём
компьютере. SSH-сеанс должен оставаться открытым во время работы клиента.

## 3. Отправьте запрос

Все запросы требуют заголовок авторизации:

```http
Authorization: Bearer <NETWORK_API_TOKEN>
```

Создайте задачу:

```bash
curl -X POST http://127.0.0.1:8765/v1/requests \
  -H 'Authorization: Bearer <NETWORK_API_TOKEN>' \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "client-0001",
    "text": "Что сейчас по проекту X?",
    "client": "external-client"
  }'
```

Ответ содержит `job_id`:

```json
{
  "job_id": "...",
  "request_id": "client-0001",
  "status": "queued"
}
```

`request_id` должен быть уникальным для одного запроса. Повторная отправка с
тем же идентификатором возвращает существующую задачу и не запускает действие
заново.

## 4. Получите результат

Проверяйте состояние задачи:

```bash
curl http://127.0.0.1:8765/v1/jobs/<job_id> \
  -H 'Authorization: Bearer <NETWORK_API_TOKEN>'
```

Возможные состояния: `queued`, `running`, `completed`, `failed`.

После `completed` поле `result.report` содержит текстовый ответ. Если сервер
создал итоговый файл, в `result.artifacts` появится список с именем файла и
`download_url`:

```bash
curl -L http://127.0.0.1:8765/v1/jobs/<job_id>/artifacts/artifact-0 \
  -H 'Authorization: Bearer <NETWORK_API_TOKEN>' \
  -o result.bin
```

Сервер выдаёт только файлы, которые текущий workflow объявил итоговыми. Пути
к произвольным файлам vault через этот API не принимаются.

## 5. Файловый API

Файловый API поддерживает каталог, чтение и изменение существующих файлов.
Создание, удаление и переименование пока не поддерживаются.

```http
GET /v1/vault/tree?path=projects&depth=1&page_size=100
GET /v1/vault/file?path=projects/example.md
PUT /v1/vault/file?path=projects/example.md
POST /v1/vault/conflicts/<conflict_id>/resolve
```

Каталог показывает только корни `user_content_roots` из `vault-manifest.json`.
Служебные каталоги, символические ссылки, специальные файлы, абсолютные пути,
обратные слеши и пути с `..` не выдаются. `depth` принимает значения от 0 до
8, `page_size` — от 1 до 1000. Для следующей страницы передавайте непрозрачный
`page_token` из предыдущего ответа.

Пример каталога:

```bash
curl 'http://127.0.0.1:8765/v1/vault/tree?path=projects&depth=1' \
  -H 'Authorization: Bearer <NETWORK_API_TOKEN>'
```

Чтение файла возвращает исходные байты, размер, MIME-тип, время изменения и
`ETag` — SHA-256 полного содержимого. Текстовые и бинарные файлы не
декодируются и не преобразуются.

```bash
curl -D headers.txt \
  'http://127.0.0.1:8765/v1/vault/file?path=projects/example.md' \
  -H 'Authorization: Bearer <NETWORK_API_TOKEN>' \
  -o example.md
```

Для сохранения существующего файла передайте `If-Match` из заголовка `ETag`:

```bash
curl -X PUT \
  'http://127.0.0.1:8765/v1/vault/file?path=projects/example.md' \
  -H 'Authorization: Bearer <NETWORK_API_TOKEN>' \
  -H 'If-Match: "<sha256>"' \
  -H 'Content-Type: application/octet-stream' \
  --data-binary @example.md
```

Сервер повторно проверяет `ETag` непосредственно перед атомарной записью под
блокировкой vault.

Если `If-Match` отсутствует, сервер возвращает `428`. Если файл изменился,
сервер возвращает `409 Conflict` с `conflict_id`, `expected_etag`,
`current_etag` и `proposed_etag`. Клиент должен показать пользователю выбор
«заменить» или «отменить»:

```bash
curl -X POST \
  'http://127.0.0.1:8765/v1/vault/conflicts/<conflict_id>/resolve' \
  -H 'Authorization: Bearer <NETWORK_API_TOKEN>' \
  -H 'Content-Type: application/json' \
  -d '{"decision":"replace"}'
```

Для отмены передайте `{"decision":"cancel"}`. Решение одноразовое и
привязано к пути, текущему отпечатку и предложенным байтам. Если файл изменился
ещё раз, сервер создаст новый конфликт. Слишком большой файл получает `413`.

## 6. Поиск через QMD

Сетевой поиск выполняется через локальный индекс QMD и всегда возвращает не
более 20 результатов. Параметр `limit` отсутствует и не настраивается клиентом.

```bash
curl 'http://127.0.0.1:8765/v1/vault/search?q=проект%20НПД' \
  -H 'Authorization: Bearer <NETWORK_API_TOKEN>'
```

Ответ содержит канонический относительный путь файла, каталог, имя, заголовок и
фрагмент:

```json
{
  "query": "проект НПД",
  "source": "qmd",
  "total": 2,
  "limited": false,
  "results": [
    {
      "path": "projects/npd/status.md",
      "directory": "projects/npd",
      "name": "status.md",
      "title": "Статус проекта НПД",
      "snippet": "Текущий статус проекта..."
    }
  ]
}
```

Если результатов больше 20, возвращаются первые 20 и `limited: true`. Поиск не
обновляет индекс и не изменяет vault. Для открытия найденного файла используйте
`GET /v1/vault/file?path=...`.

## Ошибки

- `401` — отсутствует или неверен bearer-токен;
- `400` — некорректный JSON, пустой `text` или `request_id`;
- `403` — путь не входит в пользовательские корни manifest;
- `409` — файл изменился или конфликт уже устарел;
- `411` — отсутствует `Content-Length`;
- `413` — тело запроса или файл слишком велики;
- `428` — при сохранении не передан `If-Match`;
- `503` — QMD недоступен или не смог выполнить поиск;
- `404` — неизвестная задача или артефакт;
- `failed` — workflow завершился ошибкой, подробность находится в результате
  задачи.

Если сервис не отвечает, сначала проверьте SSH-туннель, затем:

```bash
systemctl --user status a-second-brain-network.service --no-pager
journalctl --user -u a-second-brain-network.service -n 100 --no-pager
```
