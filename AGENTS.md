# Yandex Contest Manager — AGENTS.md

## Project Overview

Скрипты для работы с Crash-отправлениями в Яндекс.Контесте.

### make_ignored.py — массовое игнорирование старых Crash
- Получает все отправления контеста через публичный API v2
- Фильтрует только Crash
- Группирует по (пользователь, задача)
- Помечает все Crash как Ignored, **кроме** самого последнего по времени
- Генерирует JavaScript для вставки в консоль админ-панели (т.к. публичный API не позволяет менять вердикт)

### crash_viewer.py — просмотр и скачивание последних Crash с исходным кодом
- Обратный фильтр: для каждого (пользователь, задача) оставляет **только** самую последнюю Crash-посылку
- Исключает пользователей, у которых есть ЛЮБАЯ посылка за последние N часов (по умолчанию 2.5) — они ещё участвуют
- Скачивает исходный код посылок через `/contests/{contestId}/submissions/{submissionId}/source`
- Бинарные/файловые посылки пропускаются
- `--save` — сохраняет файлы как `<submission_id>` в текущий каталог (без расширения); имя файла = runId в админ-панели
- `--dry-run` — показать список посылок без загрузки исходников

### download_contest.sh — обёртка для скачивания всех задач
- Создаёт структуру каталогов `tasks/<alias>/solutions/` + пустой `statement.md`
- Для каждой задачи вызывает `crash_viewer.py --save --problem <alias>` в её каталоге `solutions/`
- Если `--problem` не указан — автоматически определяет список задач через dry-run

### review_contest.py — автоматическое ревью через OpenAI-совместимое API
- Для каждого решения в `tasks/<alias>/solutions/` отправляет запрос к OpenAI-совместимому API: системный промпт из `review_prompt.md`, условие и исходник — в user-сообщении
- Разбирает JSON-ответ модели (`percent`, `summary`, `remarks`); до 3 попыток при неразборчивом ответе
- Решения с `percent ≥ 85` автоматически помечаются OK (без запроса оператору), `percent ≤ 15` (WA) и промежуточные показываются для интерактивного подтверждения
- Интерактивный интерфейс: `+` → OK, `-` → WA, Enter → пропустить (ручная проверка), `q` → выход
- Генерирует JS для админ-панели и URL-адреса для ручной проверки
- Удаляет файлы решений, для которых вердикт применён (OK/WA); отложенные на ручную проверку переносятся в `tasks/<alias>/manual/`; `--keep-files` отключает удаление и перенос
- Требует переменную окружения `REVIEW_API_KEY`

## Yandex Contest API v2 (Public)

### Authentication
OAuth token в заголовке `Authorization: OAuth {token}`. Переменная окружения: `YANDEX_CONTEST_TOKEN`.

### Pagination
**Нумерация страниц 1-базированная!** `page=0` вызывает HTTP 500.

### Key Endpoints

- `GET /contests/{contestId}` — информация о контесте (не критична при ошибке)
- `GET /contests/{contestId}/submissions?page=1&pageSize=100` — посылки с пагинацией. Response: `{"count": N, "submissions": [...]}`
- `GET /contests/{contestId}/submissions/{submissionId}/source` — исходный код, возвращает `application/octet-stream` (сырые байты, НЕ JSON). Нельзя использовать `api.request()` — нужен прямой запрос через `api.session.request()` с `headers={"Accept": "*/*"}` и ручным `response.read()`. Пустой ответ (`b""`) — валиден. Бинарные посылки определяются по нулевым байтам или ошибке UTF-8.
- `POST /submissions/{submissionId}/rejudge` — перезапуск проверки

### Important Notes
- `problemId` — строка, не число
- Вердикт — строка, сравнивать case-insensitive
- При равенстве `submissionTime` — все такие посылки считаются «последними», выводится предупреждение
- HTTP 5xx ретраятся с экспоненциальной задержкой (3 повторных попытки, 1с → 2с → 4с; всего до 4 запросов)
- **Изменить вердикт через публичный API нельзя** — только через админ-панель

## Admin API (Yandex Contest Admin Panel)

Смена вердикта возможна только через внутренний API админ-панели. Скрипт не может вызывать его напрямую (нужен CSRF-токен сессии браузера), поэтому генерирует JavaScript для вставки в DevTools-консоль.

```
PATCH /api/admin/contest/{contestId}/submission/verdict
     ?verdict=Ignored
     &filter=runId%3D{submissionId}
```

CSRF-токен извлекается из `<meta name="secretkey">` или cookie, заголовок: `x-csrf-token`. `submissionId` из публичного API = `runId` в фильтре админ-панели.

## Running

```bash
# make_ignored.py — сгенерировать JS для админ-панели:
python make_ignored.py CONTEST_ID
python make_ignored.py CONTEST_ID --delay 1000 --concurrency 5

# crash_viewer.py — просмотр/скачивание Crash-посылок:
python crash_viewer.py CONTEST_ID
python crash_viewer.py CONTEST_ID --problem A --problem B
python crash_viewer.py CONTEST_ID --save
python crash_viewer.py CONTEST_ID --dry-run --hours 3

# download_contest.sh — скачивание всех задач:
./download_contest.sh CONTEST_ID
./download_contest.sh CONTEST_ID --problem A --problem B

# review_contest.py — ревью через OpenAI-совместимое API:
export REVIEW_API_KEY="..."
python review_contest.py CONTEST_ID
python review_contest.py CONTEST_ID --model MODEL --base-url URL
python review_contest.py CONTEST_ID --problem A --dry-run --keep-files
python review_contest.py CONTEST_ID --no-report
```

Требования: Python 3.12+, aiohttp.

## Code Architecture

### api.py — общий модуль для работы с API Яндекс.Контеста
Содержит `YandexContestAPI` (auth, request с retry, pagination), константы (`BASE_URL`, `TOKEN`, `CONCURRENCY`, `CRASH_VERDICT`), хелперы для извлечения полей из JSON посылки, `parse_iso_time`, `sort_key`.

### make_ignored.py — массовое игнорирование Crash
Импортирует из `api.py`.

### crash_viewer.py — просмотр и скачивание Crash
Импортирует из `api.py`.

Фильтр `--problem`: числовой ID → `problemId`, буквенный alias → `problemAlias`. Можно смешивать.

### download_contest.sh — обёртка
Shell-скрипт: dry-run → парсинг списка задач → `mkdir -p` + `crash_viewer.py --save --problem`.

### review_contest.py — оркестратор ревью
Не делает запросов к API Яндекс.Контеста. Работает с локальными файлами и отправляет запросы к OpenAI-совместимому API.

**Поток данных:** `discover_units()` → `review_all()` (конкурентно через `asyncio.gather`) → `interactive_review()` → `emit_admin_js_split()` + `emit_manual_urls()` → `write_report()` → `cleanup(ok+wa)` + `park_manual(manual)`.

**Вердикт модели:** JSON `{"percent": 0-100, "summary": "...", "remarks": [...]}`. Разбор через `parse_verdicts` с fallback-стратегиями и `validate_payload` (coercion типов).

**Пороговые значения:** `CONF_THRESHOLD = 15.0` — `percent ≤ 15` → рекомендация WA (требует подтверждения оператором), `percent ≥ (100 - 15) = 85` → автоматически OK (без запроса оператору), промежуток → "N/A" (ручная проверка).

**Ключевые константы:** `REQUEST_TIMEOUT`, `MAX_ATTEMPTS`, `API_CONCURRENCY`, `API_MAX_RETRIES`, `API_RETRY_DELAY`, `RETRY_BACKOFF`, `RETRY_AFTER_CAP`, `CANCEL_REASON` — значения в коде.

**Отчёт:** JSON-файл `review_report.json` (по умолчанию) записывается до мутации файлов; содержит все вердикты (ok/wa/manual/skipped) и ошибки. `--report PATH` — путь, `--no-report` — не писать.

**MODEL_OPTIONS:** `{"temperature": 0, "reasoning_effort": "low", "prompt_cache_key": "ya-review"}`.

**Ctrl+C:** Первый → отмена in-flight запросов через `task.cancel()` + `cancel.set()`, ожидающие задачи → `Failure(CANCEL_REASON)`. Второй → `os._exit(130)`. После `review_all` обработчик SIGINT убирается, восстанавливается стандартный `KeyboardInterrupt` для `interactive_review`.

**Таймаут:** `TimeoutError` не ретраится внутри `call_model` — повтор на уровне `review_unit`, которая владеет бюджетом попыток. Лог таймаута содержит alias, номер попытки, timeout, endpoint и модель.

**`emit_admin_js_split`:** `submission_id` берётся **только** из `unit` (имя файла), никогда из ответа модели.

**Код возврата:** 0 — успех, 1 — ошибка/нет решений, 2 — прерван интерактивный режим (есть пропущенные).
