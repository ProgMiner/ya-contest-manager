#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Автоматическое ревью решений контеста через OpenAI-совместимое API.

Скрипт автоматизирует ручную проверку Crash-посылок:

1. Находит решения в структуре tasks/<alias>/solutions/ (создаваемой
   download_contest.sh). Имя файла решения = submission_id посылки
   (= runId в админ-панели), поэтому связь с посылкой не нуждается
   в метаданных.
2. Для каждого решения отправляет запрос к OpenAI-совместимому API:
   системный промпт из review_prompt.md, условие и исходник — в user-сообщении.
3. Разбирает JSON-ответ модели: объект с полями percent,
   summary, remarks.
4. Показывает вердикты от лучшего к худшему и спрашивает оператора:
   применить вердикт (OK/WrongAnswer), отправить на ручную проверку
   или пропустить (quit).
5. Генерирует JavaScript для админ-панели (как make_ignored.py)
   и ссылки на посылки для ручной проверки.
6. Удаляет обработанные решения.

Использование:
    export REVIEW_API_KEY="..."
    python review_contest.py CONTEST_ID
    python review_contest.py CONTEST_ID --dry-run
    python review_contest.py CONTEST_ID --problem A --problem B
"""

import argparse
import asyncio
import json
import math
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import aiohttp

# LineTooLong не реэкспортируется из aiohttp/__init__.py, но путь стабилен:
# readline() поднимает её, если строка SSE длиннее 2*read_bufsize (512 КиБ).
from aiohttp.http_exceptions import LineTooLong


# ----------------------------------------------------------------------
# Константы
# ----------------------------------------------------------------------

TASKS_DIR_DEFAULT = "./tasks"
PROMPT_FILE_DEFAULT = "review_prompt.md"
MAX_ATTEMPTS = 3
INTER_CHUNK_TIMEOUT = 10     # макс. пауза между чанками SSE, с
FIRST_CHUNK_TIMEOUT = 120    # бюджет до первого чанка (очередь + prefill), с
CONNECT_TIMEOUT = 30         # TCP-connect / получение соединения из пула, с
MAX_CONTENT_CHARS = 10_000   # предел накопленного content (защита от зацикливания)
MAX_REASONING_CHARS = 50_000 # предел накопленного reasoning (защита от зацикливания)
SSE_DATA_PREFIX = b"data:"
SSE_DONE = b"[DONE]"
REVIEW_API_KEY_ENV = "REVIEW_API_KEY"
DEFAULT_BASE_URL = "https://polza.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash-0731"
ERROR_BODY_CHARS = 500
RETRY_BACKOFF = 2.0
CONF_THRESHOLD = 15.0
VERDICT_OK = "OK"
VERDICT_WA = "WrongAnswer"
ADMIN_SUBMISSION_URL = "https://admin.contest.yandex.ru/submissions/{submission_id}"

MODEL_OPTIONS = {
    "temperature": 0.3,
    "reasoning_effort": "low",
    "prompt_cache_key": "ya-review",
    # "max_completion_tokens": 1000,
    # "max_output_tokens": 1000,
}

REASONING_THRESHOLD = 1000

# Границы percent при валидации
PERCENT_MIN = 0.0
PERCENT_MAX = 100.0
# Ключи wrapper-объектов, встречающихся в ответах моделей
WRAPPER_KEYS = ("verdicts", "results", "reviews", "items")
# Длина превью неразобранного ответа модели
RAW_PREVIEW_CHARS = 400

API_CONCURRENCY = 10        # одновременных запросов к модели
API_MAX_RETRIES = 3         # повторов на 429/5xx внутри call_model
API_RETRY_DELAY = 1.0       # начальная задержка backoff, с (1с → 2с → 4с)
RETRY_AFTER_CAP = 60.0      # максимальная задержка Retry-After, с
CANCEL_REASON = "отменено пользователем (Ctrl+C)"


# ----------------------------------------------------------------------
# Модель данных
# ----------------------------------------------------------------------


@dataclass
class SolutionUnit:
    """Одно решение: файл исходника + условие + связь с посылкой."""

    alias: str
    solution_path: Path
    statement_path: Path
    submission_id: int
    problem_id: str | None


@dataclass
class Verdict:
    """Разобранный вердикт модели по одному решению."""

    unit: SolutionUnit
    percent: float
    summary: str
    remarks: list[str]
    attempts: int


@dataclass
class Failure:
    """Проблема, не позволившая проверить решение (или задача целиком)."""

    alias: str
    submission_id: int | None
    path: Path | None
    reason: str


class ModelCallError(RuntimeError):
    """Запрос к модели не выполнен (HTTP-ошибка, сеть, битый ответ)."""

    def __init__(self, message, streams=None):
        super().__init__(message)
        self.streams = streams


# ----------------------------------------------------------------------
# Обнаружение решений
# ----------------------------------------------------------------------


def discover_units(
    tasks_dir: Path,
    aliases: list[str] | None = None,
) -> list[SolutionUnit]:
    """
    Обойти tasks_dir/*/ и собрать все решения.

    Для каждого alias (подкаталог tasks/):
    - обязателен непустой statement.md;
    - обязателен каталог solutions/;
    - файлы в solutions/ должны иметь числовые имена (= submission_id,
      он же runId в админ-панели).

    Возвращает список SolutionUnit.
    """

    if not tasks_dir.is_dir():
        print(
            f"Ошибка: каталог задач не найден: {tasks_dir}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    units: list[SolutionUnit] = []

    for entry in sorted(tasks_dir.iterdir()):
        if not entry.is_dir():
            continue

        alias = entry.name

        if aliases and alias not in aliases:
            continue

        statement_path = entry / "statement.md"
        solutions_dir = entry / "solutions"

        if not statement_path.is_file() or statement_path.stat().st_size == 0:
            print(
                f"Пропуск задачи {alias}: "
                "statement.md отсутствует или пуст"
            )
            continue

        if not solutions_dir.is_dir():
            print(
                f"Пропуск задачи {alias}: "
                "отсутствует каталог solutions/"
            )
            continue

        for solution in sorted(solutions_dir.iterdir()):
            # Пропускаем dot-файлы, подкаталоги и нечисловые имена
            if solution.name.startswith("."):
                continue
            if not solution.is_file():
                continue
            if not solution.name.isdigit():
                continue

            submission_id = int(solution.name)

            units.append(
                SolutionUnit(
                    alias=alias,
                    solution_path=solution,
                    statement_path=statement_path,
                    submission_id=submission_id,
                    problem_id=None,
                )
            )

    return units


# ----------------------------------------------------------------------
# Вызов модели
# ----------------------------------------------------------------------


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After в секундах; HTTP-date не поддерживается -> None."""
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if 0.0 <= seconds <= RETRY_AFTER_CAP else None


async def _read_sse_content(
    response: aiohttp.ClientResponse,
    inter_chunk_timeout: float,
    first_chunk_timeout: float,
    cancel: asyncio.Event | None,
    label: str = "",
    reasoning_debug: bool = False,
) -> tuple[str, dict[str, str] | None]:
    """Собрать SSE-поток. Возвращает кортеж (content, streams).

    content — сконкатенированный ключ "content" (str, "" если нет).
    streams — словарь накопленных потоков (dict[str, str]) или None
    (если reasoning_debug=True и потоки уже выведены на экран).

    Накапливает все строковые ключи delta/message в streams: dict[str, str].
    Ключ "content" — основной, возвращается как результат. Non-content
    потоки с "reasoning" в имени ключа поблочно выводятся при
    reasoning_debug=True и превышении REASONING_THRESHOLD:
    preview-часть печатается, остаток хранится до следующего порога;
    в конце выводится непревышавший хвост. При reasoning_debug=False
    reasoning-потоки молча накапливаются в streams без печати.

    Пауза между чанками > inter_chunk_timeout -> TimeoutError
    (ретраем владеет review_unit). Если cancel установлен, чтение
    прекращается (break); вызывающий (call_model) проверяет cancel
    отдельно и возвращает "".
    """
    streams: dict[str, str] | None = {}
    reasoning_chars = 0
    content_chars = 0
    got_line = False

    while True:
        # Мягкая отмена: между чанками, без CancelledError
        if cancel is not None and cancel.is_set():
            break

        budget = inter_chunk_timeout if got_line else first_chunk_timeout
        try:
            line = await asyncio.wait_for(
                response.content.readline(),
                timeout=budget,
            )
        except LineTooLong as exc:
            raise ModelCallError(
                f"строка SSE длиннее лимита: {exc}",
                streams=streams,
            ) from exc

        if not line:            # EOF: b"" — поток закончился без [DONE]
            break

        stripped = line.strip()
        if not stripped:                                  # разделитель "\n"
            continue
        if not stripped.startswith(SSE_DATA_PREFIX):      # ": ping", "event:", "id:"
            continue

        payload = stripped[len(SSE_DATA_PREFIX):].strip()
        if payload == SSE_DONE:
            break

        try:
            chunk = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue                                      # битый чанк не фатален
        if not isinstance(chunk, dict):
            continue

        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            error = chunk.get("error")
            if error:
                raise ModelCallError(
                    f"Ошибка API в потоке: {error}",
                    streams=streams,
                )
            continue                                      # напр. чанк только с usage

        # Модель жива: любой чанк с choices подтверждает активность,
        # включая reasoning_content (DeepSeek thinking mode) — модель
        # размышляет, но ещё не выдаёт видимый ответ.
        got_line = True

        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                delta = choice.get("message")             # часть провайдеров дублирует
                if not isinstance(delta, dict):
                    continue

            # Копим все строковые ключи дельты (content, reasoning_content,
            # и т.д.) — каждый поток отдельно, для диагностики. Пустые или
            # нестроковые значения (None на finish_reason-чанке) пропускаем.
            for key, value in delta.items():
                if not isinstance(value, str) or not value:
                    continue

                if key == "content":
                    if "content" not in streams:
                        print(f"  {label}ответ...", flush=True)

                # Учитываем только content и размышления модели
                if key != "content" and "reasoning" not in key.lower():
                    continue

                new_value = streams.get(key, "") + value

                if key == "content":
                    content_chars += len(value)
                    if content_chars > MAX_CONTENT_CHARS:
                        raise ModelCallError(
                            f"content превысил {MAX_CONTENT_CHARS} символов",
                            streams=streams,
                        )
                    streams["content"] = new_value
                    continue

                # reasoning-ключи
                reasoning_chars += len(value)
                if reasoning_chars > MAX_REASONING_CHARS:
                    raise ModelCallError(
                        f"reasoning превысил {MAX_REASONING_CHARS} символов",
                        streams=streams,
                    )

                if not reasoning_debug or len(new_value) < REASONING_THRESHOLD:
                    streams[key] = new_value
                    continue

                value_preview = new_value[:REASONING_THRESHOLD]

                i = (
                    i for i in range(len(value_preview) - 1, -1, -1)
                    if value_preview[i].isspace()
                )
                i = next(i, len(value_preview))

                value_preview = value_preview[:i]
                remainder = new_value[i + 1:].lstrip()

                print(f"  {label}{key}: {value_preview}", flush=True)
                streams[key] = remainder

    content = streams.get("content", "")

    # Выводим окончания размышления, которые не выводились ранее
    if reasoning_debug:
        for key, value in streams.items():
            if key == "content":
                continue

            print(f"  {label}{key}: {value}", flush=True)

        streams = None

    return content, streams


async def call_model(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    api_key: str,
    base_url: str,
    model_id: str,
    system_prompt: str,
    user_prompt: str,
    timeout: int = INTER_CHUNK_TIMEOUT,
    max_retries: int = API_MAX_RETRIES,
    retry_delay: float = API_RETRY_DELAY,
    label: str = "",
    cancel: asyncio.Event | None = None,
    reasoning_debug: bool = False,
) -> str:
    """Запрос к модели: chat/completions (streaming).

    Возвращает текст ответа (конкатенация ключа "content").
    При пустом или отсутствующем контенте выбрасывает
    ModelCallError("Ответ модели пуст").
    """

    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        **MODEL_OPTIONS,
        "stream": True,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {api_key}",
    }
    tag = f"{label}: " if label else ""

    request_timeout = aiohttp.ClientTimeout(
        total=None,
        connect=CONNECT_TIMEOUT,
        sock_connect=CONNECT_TIMEOUT,
        sock_read=max(float(timeout), float(FIRST_CHUNK_TIMEOUT)),
    )

    text: str | None = None
    streams: dict | None = None
    last_error: ModelCallError | None = None

    for attempt in range(max_retries + 1):
        retry_after: float | None = None
        status: int | None = None
        try:
            async with semaphore:
                async with session.post(
                    url,
                    data=body,
                    headers=headers,
                    timeout=request_timeout,
                ) as response:
                    status = response.status

                    if status == 429 or status >= 500:
                        detail = (await response.text())[:ERROR_BODY_CHARS]
                        last_error = ModelCallError(
                            f"HTTP {status} от {url}: {detail}"
                        )
                        retry_after = _parse_retry_after(
                            response.headers.get("Retry-After")
                        )
                    elif status >= 400:
                        detail = (await response.text())[:ERROR_BODY_CHARS]
                        raise ModelCallError(
                            f"HTTP {status} от {url}: {detail}"
                        )
                    else:
                        text, streams = await _read_sse_content(
                            response,
                            inter_chunk_timeout=float(timeout),
                            first_chunk_timeout=float(FIRST_CHUNK_TIMEOUT),
                            cancel=cancel,
                            label=tag,
                            reasoning_debug=reasoning_debug,
                        )

        except TimeoutError:
            # Таймаут не ретраится здесь: повтор — ответственность review_unit,
            # которая владеет бюджетом попыток. Ретрай на двух уровнях даёт
            # max_attempts × max_retries × timeout (часы при timeout=1200).
            raise
        except aiohttp.ClientError as exc:
            raise ModelCallError(f"Ошибка сети: {exc}") from exc
        except OSError as exc:
            raise ModelCallError(f"Ошибка сети: {exc}") from exc

        if cancel is not None and cancel.is_set():
            return ""

        if text is not None:
            break

        if attempt < max_retries:
            delay = retry_delay * (2 ** attempt)
            if retry_after is not None:
                delay = max(delay, retry_after)
            print(
                f"  {tag}HTTP {status}, повторная попытка "
                f"{attempt + 1}/{max_retries} через {delay:.1f}с..."
            )
            await asyncio.sleep(delay)
            continue

        if last_error is not None:
            raise last_error  # исчерпаны попытки на 429/5xx
        raise ModelCallError(
            f"HTTP {status} от {url}: "
            "исчерпаны попытки, но ошибка не сохранена"
        )

    if text is None:  # страховка
        raise ModelCallError("Запрос к модели не выполнен", streams=streams)

    text = text.strip()

    if not text:
        raise ModelCallError(f"Ответ модели пуст", streams=streams)

    return text


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def build_user_prompt(unit: SolutionUnit) -> str:
    statement = read_text_file(unit.statement_path)
    solution = read_text_file(unit.solution_path)
    return f"## Problem Statement\n{statement}\n\n## Solution\n{solution}\n"


async def review_unit(
    unit: SolutionUnit,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    api_key: str,
    base_url: str,
    model_id: str,
    system_prompt: str,
    timeout: int = INTER_CHUNK_TIMEOUT,
    max_attempts: int = MAX_ATTEMPTS,
    retry_backoff: float = RETRY_BACKOFF,
    cancel: asyncio.Event | None = None,
    reasoning_debug: bool = False,
) -> Verdict | Failure:
    """
    Прогнать одно решение через модель с повторами.

    До max_attempts попыток: запрос к API, разбор ответа.
    Успех -> Verdict; исчерпание попыток -> Failure.

    Если cancel установлен, новые запросы к модели не отправляются.
    """
    label = f"{unit.alias}#{unit.submission_id}"

    try:
        user_prompt = build_user_prompt(unit)
    except OSError as exc:
        return Failure(
            alias=unit.alias,
            submission_id=unit.submission_id,
            path=unit.solution_path,
            reason=f"не удалось прочитать файлы решения: {exc}",
        )

    for attempt in range(1, max_attempts + 1):
        # Перед каждой попыткой проверяем отмену
        if cancel is not None and cancel.is_set():
            return Failure(
                alias=unit.alias,
                submission_id=unit.submission_id,
                path=unit.solution_path,
                reason=CANCEL_REASON,
            )

        text = ""
        timed_out = False
        tag = f"{label}: попытка {attempt}/{max_attempts}"

        try:
            text = await call_model(
                session, semaphore, api_key, base_url, model_id,
                system_prompt, user_prompt, timeout,
                label=tag,
                cancel=cancel,
                reasoning_debug=reasoning_debug,
            )
        except TimeoutError:
            timed_out = True
        except (ModelCallError, OSError) as exc:
            print(f"  {tag}: запрос к модели не выполнен: {exc}")

            if isinstance(exc, ModelCallError) and exc.streams is not None:
                for key, value in exc.streams.items():
                    print(f"\n--- Лог {key} ---")
                    print(value)
                    print(f"--- Конец лога ---")

        if cancel is not None and cancel.is_set():
            return Failure(
                alias=unit.alias,
                submission_id=unit.submission_id,
                path=unit.solution_path,
                reason=CANCEL_REASON,
            )

        if timed_out:
            print(
                f"  {tag}: модель молчала "
                f"дольше {timeout}с (нет чанков), endpoint {base_url}, "
                f"модель {model_id}"
            )

        parsed = parse_verdicts(text)

        if parsed is not None:
            print(
                f"  {tag}: вердикт получен "
                f"({parsed['percent']:g}%)"
            )
            return _make_verdict(unit, parsed, attempt)

        if attempt < max_attempts:
            print(
                f"  {tag}: не удалось разобрать ответ, "
                f"повтор..."
            )
            await asyncio.sleep(retry_backoff * attempt)
        else:
            print(f"  {tag}: не удалось разобрать ответ.")
            preview = text[:RAW_PREVIEW_CHARS]
            if preview:
                print(f"  Превью ответа: {preview}", file=sys.stderr)

    return Failure(
        alias=unit.alias,
        submission_id=unit.submission_id,
        path=unit.solution_path,
        reason=f"не удалось получить разбираемый вердикт "
               f"за {max_attempts} попыток",
    )


async def review_all(
    units: list[SolutionUnit],
    api_key: str,
    base_url: str,
    model_id: str,
    system_prompt: str,
    timeout: int = INTER_CHUNK_TIMEOUT,
    max_attempts: int = MAX_ATTEMPTS,
    concurrency: int = API_CONCURRENCY,
    cancel: asyncio.Event | None = None,
    reasoning_debug: bool = False,
) -> tuple[list[Verdict], list[Failure]]:
    """Прогнать все решения через модель конкурентно.

    Если cancel установлен, ожидающие задачи возвращаются как
    Failure(CANCEL_REASON), а in-flight задачи отменяются через
    task.cancel() — это прерывает ожидающие HTTP-запросы
    немедленно, без ожидания таймаута.
    """

    semaphore = asyncio.Semaphore(concurrency)
    total = len(units)
    done = 0

    async def _review_one(unit: SolutionUnit) -> Verdict | Failure:
        nonlocal done
        # Проверяем отмену перед запуском (для задач, ждущих semaphore)
        if cancel is not None and cancel.is_set():
            done += 1
            print(
                f"[{done}/{total}] {unit.alias}#{unit.submission_id}: ОТМЕНЕНО",
                flush=True,
            )
            return Failure(
                alias=unit.alias,
                submission_id=unit.submission_id,
                path=unit.solution_path,
                reason=CANCEL_REASON,
            )
        result = await review_unit(
            unit, session, semaphore, api_key, base_url, model_id,
            system_prompt, timeout=timeout, max_attempts=max_attempts,
            cancel=cancel,
            reasoning_debug=reasoning_debug,
        )
        done += 1
        kind = "вердикт" if isinstance(result, Verdict) else "ОШИБКА"
        print(
            f"[{done}/{total}] {unit.alias}#{unit.submission_id}: {kind}",
            flush=True,
        )
        return result

    async with aiohttp.ClientSession(
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        # total=None: живость контролируется inter-chunk timeout в call_model
        timeout=aiohttp.ClientTimeout(total=None),
    ) as session:
        # Создаём задачи явно, чтобы иметь возможность их отменить
        tasks = [asyncio.create_task(_review_one(u)) for u in units]

        # Watcher: при cancel отменяет все in-flight задачи
        async def _cancel_watcher():
            if cancel is None:
                return
            await cancel.wait()
            for t in tasks:
                if not t.done():
                    t.cancel()

        watcher = asyncio.create_task(_cancel_watcher())

        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass

    verdicts: list[Verdict] = []
    failures: list[Failure] = []

    for unit, result in zip(units, results):
        if isinstance(result, Verdict):
            verdicts.append(result)
        elif isinstance(result, Failure):
            failures.append(result)
        elif isinstance(result, asyncio.CancelledError):
            failures.append(Failure(
                alias=unit.alias,
                submission_id=unit.submission_id,
                path=unit.solution_path,
                reason=CANCEL_REASON,
            ))
        else:
            failures.append(Failure(
                alias=unit.alias,
                submission_id=unit.submission_id,
                path=unit.solution_path,
                reason=f"внутренняя ошибка: {result!r}",
            ))

    return verdicts, failures


def _make_verdict(unit: SolutionUnit, parsed: dict, attempts: int) -> Verdict:
    return Verdict(
        unit=unit,
        percent=parsed["percent"],
        summary=parsed["summary"],
        remarks=parsed["remarks"],
        attempts=attempts,
    )


# ----------------------------------------------------------------------
# Разбор ответа модели
# ----------------------------------------------------------------------


def _iter_fenced_blocks(text: str) -> Iterator[str]:
    """Содержимое markdown-блоков ```...```, не используя regex."""
    parts = text.split("```")
    for i, part in enumerate(parts):
        if i % 2 == 1:  # нечётные индексы = внутри блока
            lines = part.split("\n", 1)
            # Если первая строка — info string (json, python и т.п.) без {/[
            if lines and not any(c in lines[0] for c in "{["):
                yield "\n".join(lines[1:]) if len(lines) > 1 else ""
            else:
                yield part


def _flatten(obj, _depth: int = 0) -> Iterator[dict]:
    """Нормализовать ответ в поток dict'ов.

    _depth — внутренний счётчик глубины для защиты от
    бесконечной рекурсии на вложенных wrapper-объектах.
    """
    if _depth > 10:
        return
    if isinstance(obj, dict):
        # Wrapper-объект: {"verdicts": [...]}
        for key in WRAPPER_KEYS:
            if key in obj and isinstance(obj[key], list):
                yield from _flatten(obj[key], _depth + 1)
                return
        # Обычный объект с нужными ключами
        yield obj
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                yield from _flatten(item, _depth + 1)


def iter_json_candidates(text: str) -> Iterator[dict]:
    """Все JSON-объекты из ответа модели, в порядке появления.

    Три стратегии:
      1) весь ответ целиком
      2) содержимое markdown-блоков
      3) сканирование через JSONDecoder.raw_decode с каждой позиции '{' / '['
    """
    # Стратегия 1: весь текст как JSON
    try:
        obj = json.loads(text.strip())
        yield from _flatten(obj)
    except (ValueError, TypeError):
        pass

    # Стратегия 2: markdown-блоки
    for block in _iter_fenced_blocks(text):
        try:
            obj = json.loads(block.strip())
            yield from _flatten(obj)
        except (ValueError, TypeError):
            pass

    # Стратегия 3: raw_decode сканирование
    dec = json.JSONDecoder()
    i = 0
    while i < len(text):
        # Ищем следующую { или [
        brace_pos = text.find("{", i)
        bracket_pos = text.find("[", i)
        if brace_pos == -1 and bracket_pos == -1:
            break
        if brace_pos == -1:
            next_pos = bracket_pos
        elif bracket_pos == -1:
            next_pos = brace_pos
        else:
            next_pos = min(brace_pos, bracket_pos)

        try:
            obj, end = dec.raw_decode(text, next_pos)
            yield from _flatten(obj)
            i = end
        except (ValueError, TypeError):
            i = next_pos + 1


def _coerce_percent(value) -> float | None:
    """Принудить значение к percent или вернуть None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return None
        p = float(value)
        if PERCENT_MIN <= p <= PERCENT_MAX:
            return p
        return None
    if isinstance(value, str):
        s = value.strip().rstrip("%").replace(",", ".")
        try:
            p = float(s)
        except ValueError:
            return None
        if PERCENT_MIN <= p <= PERCENT_MAX:
            return p
        return None
    return None


def _coerce_remarks(value) -> list[str] | None:
    """Принудить значение к list[str] или вернуть None."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        result = []
        for item in value:
            s = str(item).strip()
            if s:
                result.append(s)
        return result
    return None


def validate_payload(payload: dict) -> dict | None:
    """Валидировать и принудить payload -> {percent, summary, remarks} или None."""
    if not isinstance(payload, dict):
        return None

    # percent
    percent = _coerce_percent(payload.get("percent"))
    if percent is None:
        return None

    # summary
    summary = payload.get("summary")
    if summary is None:
        summary = ""
    if not isinstance(summary, str):
        summary = str(summary)

    # remarks
    remarks = _coerce_remarks(payload.get("remarks"))
    if remarks is None:
        return None

    return {
        "percent": percent,
        "summary": summary.strip(),
        "remarks": remarks,
    }


def parse_verdicts(text: str) -> dict | None:
    """
    Найти JSON-объект вердикта в тексте ответа модели.

    Возвращает {"percent": float, "summary": str, "remarks": list[str]}
    либо None. Из нескольких подходящих объектов берётся последний.
    """
    candidates = []
    for payload in iter_json_candidates(text):
        validated = validate_payload(payload)
        if validated is not None:
            candidates.append(validated)

    if not candidates:
        return None

    return candidates[-1]


# ----------------------------------------------------------------------
# Решение и вывод
# ----------------------------------------------------------------------


def decide_verdict(percent: float) -> str:
    """Процент -> вердикт для админ-панели."""

    if percent <= CONF_THRESHOLD:
        return VERDICT_WA

    if 100 - percent <= CONF_THRESHOLD:
        return VERDICT_OK

    return "N/A"


def sort_verdicts(verdicts: list[Verdict]) -> list[Verdict]:
    """От лучшего к худшему: по убыванию процента, затем alias, submission_id."""

    return sorted(
        verdicts,
        key=lambda v: (-v.percent, v.unit.alias, v.unit.submission_id),
    )


def interactive_review(
    verdicts: list[Verdict],
) -> tuple[list[Verdict], list[Verdict], list[Verdict], list[Verdict]]:
    """
    Интерактивное подтверждение вердиктов.

    Для каждого вердикта (от лучшего к худшему) оператор выбирает:
    - +  — OK (пометить как OK, независимо от рекомендации модели);
    - -  — WA (пометить как WrongAnswer, независимо от рекомендации);
    - Enter (пустой ввод) — пропустить (ручная проверка);
    - q  — выйти из цикла (остальное — skipped).

    Возвращает (ok, wa, manual, skipped).
    """

    ok_list: list[Verdict] = []
    wa_list: list[Verdict] = []
    manual: list[Verdict] = []
    skipped: list[Verdict] = []

    ordered = sort_verdicts(verdicts)
    total = len(ordered)

    for index, verdict in enumerate(ordered, start=1):
        unit = verdict.unit
        decision = decide_verdict(verdict.percent)

        try:
            source = read_text_file(unit.solution_path)
        except OSError:
            source = "<ошибка чтения файла>"

        remarks = "".join([f"    - {x}\n" for x in verdict.remarks])

        prompt = (
            "\n\n"
            f"[{index}/{total}] Задача {unit.alias}, "
            f"посылка #{unit.submission_id}\n"
            f"  Рекомендация: {decision} ({verdict.percent:g}%)\n"
            f"  Итог: {verdict.summary}\n"
            f"  Замечания: \n{remarks}"
            f"  Submission: "
            f"{ADMIN_SUBMISSION_URL.format(submission_id=unit.submission_id)}\n"
            f"\n"
            f"--- Исходный код ---\n"
            f"{source}\n"
            f"--- Конец кода ---\n"
            f"\n"
            f"[+] OK / [-] WA / [Enter] Пропустить / [q] Выход: "
        )

        while True:
            try:
                answer = input(prompt).strip().lower()
            except EOFError:
                answer = "q"
            except KeyboardInterrupt:
                print()  # новая строка после ^C
                answer = "q"

            if answer == "+":
                ok_list.append(verdict)
                break

            if answer == "-":
                wa_list.append(verdict)
                break

            if answer == "":
                manual.append(verdict)
                break

            if answer in ("q", "quit"):
                skipped.extend(ordered[index - 1:])
                return ok_list, wa_list, manual, skipped

            # Неизвестный вариант — переспрашиваем
            prompt = (
                "  Введите «+» (OK), «-» (WrongAnswer), "
                "Enter (пропустить) или «q» (выйти)."
            )

    return ok_list, wa_list, manual, skipped


def emit_admin_js_split(
    contest_id: int,
    ok_verdicts: list[Verdict],
    wa_verdicts: list[Verdict],
    delay_ms: int,
) -> None:
    """
    Сгенерировать один JavaScript для админ-панели.

    Все решения (OK и WrongAnswer) попадают в один скрипт,
    где каждый runId идёт со своим вердиктом.
    """

    # Собираем пары (runId, verdict) — сначала OK, потом WA
    pairs: list[tuple[int, str]] = []
    for v in ok_verdicts:
        pairs.append((v.unit.submission_id, VERDICT_OK))
    for v in wa_verdicts:
        pairs.append((v.unit.submission_id, VERDICT_WA))

    if not pairs:
        return

    pairs_json = json.dumps(pairs)

    js = f"""\
(async () => {{
  const contestId = {contest_id};
  const pairs = {pairs_json};
  const delayMs = {delay_ms};

  // Получаем CSRF-токен из мета-тега (secretkey) или cookie
  function getCsrfToken() {{
    const meta = document.querySelector('meta[name="secretkey"]');
    if (meta) return meta.getAttribute('content');

    // Fallback: пробуем извлечь из cookie
    const match = document.cookie.match(/(?:XSRF-TOKEN|csrf_token)=([^;]+)/);
    if (match) return decodeURIComponent(match[1]);

    return null;
  }}

  const csrfToken = getCsrfToken();
  if (!csrfToken) {{
    console.error('CSRF-токен не найден. Убедитесь, что вы на странице admin.contest.yandex.ru');
    return;
  }}

  console.log(`Начинаю установку вердиктов для ${{pairs.length}} посылок...`);
  console.log(`CSRF-токен: ${{csrfToken.substring(0, 8)}}...`);

  let ok = 0;
  let fail = 0;

  for (const [runId, verdict] of pairs) {{
    const url = `/api/admin/contest/${{contestId}}/submission/verdict`
      + `?verdict=${{encodeURIComponent(verdict)}}`
      + `&filter=${{encodeURIComponent('runId=' + runId)}}`;

    try {{
      const resp = await fetch(url, {{
        method: 'PATCH',
        headers: {{
          'Accept': 'application/json, text/plain, */*',
          'x-csrf-token': csrfToken,
        }},
      }});

      if (resp.ok) {{
        ok++;
        console.log(`✓ #${{runId}} -> ${{verdict}}`);
      }} else {{
        fail++;
        const text = await resp.text();
        console.error(`✗ #${{runId}}: HTTP ${{resp.status}} ${{text}}`);
      }}
    }} catch (err) {{
      fail++;
      console.error(`✗ #${{runId}}: ${{err.message}}`);
    }}

    // Задержка чтобы не перегружать сервер
    if (runId !== pairs[pairs.length - 1][0]) {{
      await new Promise(r => setTimeout(r, delayMs));
    }}
  }}

  console.log(`\\nГотово: ${{ok}} успешно, ${{fail}} с ошибкой из ${{pairs.length}}`);
}})();"""

    print(js)


def emit_manual_urls(manual: list[Verdict]) -> None:
    """Вывести ссылки на посылки, которые нужно проверить вручную."""

    for verdict in manual:
        print(
            ADMIN_SUBMISSION_URL.format(
                submission_id=verdict.unit.submission_id,
            )
        )


def cleanup(
    decided: list[Verdict],
    failures: list[Failure],
    manual: list[Verdict] | None = None,
) -> None:
    """
    Удалить файлы решений, для которых принято решение.

    Неразобранные (failures) и пропущенные (quit) файлы не трогаются.
    Решения на ручную проверку (manual) также удаляются — оператор
    посмотрит их в интерфейсе Яндекс.Контест по выданным ссылкам.
    """

    # failures учитывается в итоговом отчёте, здесь не нужен
    del failures

    to_delete = decided
    if manual:
        to_delete = decided + manual

    for verdict in to_delete:
        path = verdict.unit.solution_path
        path.unlink(missing_ok=True)
        print(
            f"Удалён файл {path} "
            f"(посылка {verdict.unit.submission_id})"
        )


# ----------------------------------------------------------------------
# Точка входа
# ----------------------------------------------------------------------


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Автоматическое ревью решений контеста: "
            "оценка Crash-посылок, генерация JS для админ-панели "
            "и очистка обработанных файлов."
        ),
    )

    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=(
            "ID модели для OpenAI-совместимого API "
            f"(по умолчанию {DEFAULT_MODEL})"
        ),
    )

    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Base URL OpenAI-совместимого API (по умолчанию {DEFAULT_BASE_URL})",
    )

    parser.add_argument(
        "contest_id",
        type=int,
        help=(
            "ID контеста — нужен для генерации "
            "JavaScript для админ-панели"
        ),
    )

    parser.add_argument(
        "--tasks-dir",
        default=TASKS_DIR_DEFAULT,
        help=(
            "Корневой каталог задач со структурой "
            "tasks/<alias>/statement.md + solutions/ "
            f"(по умолчанию {TASKS_DIR_DEFAULT})"
        ),
    )

    parser.add_argument(
        "--problem",
        action="append",
        default=[],
        help=(
            "Проверять только задачи с указанным alias; "
            "можно указывать несколько раз"
        ),
    )

    parser.add_argument(
        "--prompt-file",
        default=PROMPT_FILE_DEFAULT,
        help=(
            "Файл с системным промптом "
            f"(по умолчанию {PROMPT_FILE_DEFAULT})"
        ),
    )

    parser.add_argument(
        "--delay",
        type=int,
        default=500,
        help=(
            "Задержка между запросами в генерируемом JS, мс "
            "(по умолчанию 500)"
        ),
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=INTER_CHUNK_TIMEOUT,
        help=(
            "Максимальная пауза между чанками потокового ответа модели, "
            "секунды; при превышении попытка считается провалившейся "
            f"(по умолчанию {INTER_CHUNK_TIMEOUT})"
        ),
    )

    parser.add_argument(
        "--attempts",
        type=int,
        default=MAX_ATTEMPTS,
        help=(
            "Максимальное число попыток получить разбираемый вердикт "
            f"(по умолчанию {MAX_ATTEMPTS})"
        ),
    )

    parser.add_argument(
        "--concurrency",
        type=int,
        default=API_CONCURRENCY,
        help=(
            "Максимальное количество одновременных запросов к модели "
            f"(по умолчанию {API_CONCURRENCY})"
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Показать параметры запросов к модели, не отправлять",
    )

    parser.add_argument(
        "--keep-files",
        action="store_true",
        default=False,
        help="Не удалять файлы решений после ревью",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Выводить поток размышлений модели (reasoning_content) на экран",
    )

    args = parser.parse_args()

    # Проверяем API-ключ
    api_key = os.environ.get(REVIEW_API_KEY_ENV, "").strip()
    if not api_key:
        print(
            f"Ошибка: не задана переменная окружения {REVIEW_API_KEY_ENV}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Проверяем файл промпта
    prompt_file = Path(args.prompt_file)
    if not prompt_file.is_file():
        print(
            f"Ошибка: файл промпта не найден: {args.prompt_file}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        system_prompt = read_text_file(prompt_file)
    except OSError as exc:
        print(
            f"Ошибка: не удалось прочитать {args.prompt_file}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    if not system_prompt.strip():
        print(
            f"Ошибка: файл промпта пуст: {args.prompt_file}",
            file=sys.stderr,
        )
        sys.exit(1)

    tasks_dir = Path(args.tasks_dir)

    # Обнаруживаем решения
    units = discover_units(tasks_dir, args.problem)

    if not units:
        print("Решений для проверки не найдено.")
        sys.exit(1)

    attempts = max(1, args.attempts)
    concurrency = max(1, args.concurrency)

    print(f"Endpoint: {args.base_url}")
    print(f"Модель: {args.model}")
    print(f"Контест: {args.contest_id}")
    print(f"Каталог задач: {tasks_dir}")
    print(f"Системный промпт: {args.prompt_file} ({len(system_prompt)} символов)")
    print(f"Найдено решений: {len(units)}")
    print(f"Конкурентность: {concurrency}")
    print(f"Пауза между чанками: {args.timeout}с")

    if args.dry_run:
        print("\nЗапросы к модели (dry-run, выполнение пропущено):")
        for unit in units:
            print(f"# {unit.alias} / submission #{unit.submission_id}")
            print(f"  POST {args.base_url.rstrip('/')}/chat/completions")
            print(f"  model: {args.model}")
            print(f"  system: {len(system_prompt)} символов ({args.prompt_file})")
            try:
                user_prompt = build_user_prompt(unit)
            except OSError as exc:
                print(f"  user: ОШИБКА чтения файлов: {exc}")
                continue
            print(
                f"  user: {len(user_prompt)} символов "
                f"(условие {unit.statement_path}, "
                f"решение {unit.solution_path})"
            )
        return

    # Запускаем ревью конкурентно
    print(f"\nЗапускаю ревью {len(units)} решений")
    print("Нажмите Ctrl+C, чтобы отменить оставшиеся запросы "
          "и перейти к проверке уже полученных результатов.")

    cancel = asyncio.Event()
    sigint_received = False

    loop = asyncio.get_running_loop()

    def _sigint_handler():
        nonlocal sigint_received
        if sigint_received:
            # Второй Ctrl+C → принудительный выход
            print("\nПринудительное завершение.", file=sys.stderr)
            os._exit(130)
        sigint_received = True
        print("\nПолучен Ctrl+C, завершаю текущие запросы...")
        cancel.set()

    loop.add_signal_handler(signal.SIGINT, _sigint_handler)

    try:
        verdicts, failures = await review_all(
            units,
            api_key,
            args.base_url,
            args.model,
            system_prompt,
            timeout=args.timeout,
            max_attempts=attempts,
            concurrency=concurrency,
            cancel=cancel,
            reasoning_debug=args.debug,
        )
    finally:
        # Убираем обработчик — для interactive_review SIGINT
        # должен порождать KeyboardInterrupt (default behaviour)
        loop.remove_signal_handler(signal.SIGINT)

    if sigint_received:
        cancelled_count = sum(1 for f in failures if CANCEL_REASON in f.reason)
        other_failures = len(failures) - cancelled_count
        print(
            f"\nОтменено: {cancelled_count}, "
            f"получено вердиктов: {len(verdicts)}, "
            f"ошибок: {other_failures}"
        )

    if not verdicts:
        print("\nНе удалось получить ни одного вердикта.")
        print_failures(failures)
        sys.exit(1)

    # Интерактивное подтверждение
    ok_verdicts, wa_verdicts, manual, skipped = interactive_review(verdicts)

    # JS для админ-панели и ссылки для ручной проверки
    if ok_verdicts or wa_verdicts:
        print("\nJavaScript для админ-панели:")
        emit_admin_js_split(args.contest_id, ok_verdicts, wa_verdicts, args.delay)

    if manual:
        print("\nПосылки на ручную проверку:")
        emit_manual_urls(manual)

    applied = ok_verdicts + wa_verdicts

    # Удаляем обработанные решения (OK/WA + ручная проверка)
    if not args.keep_files:
        if applied or manual:
            print("\nОчистка обработанных решений:")
            cleanup(applied, failures, manual=manual)
        else:
            print("\nРешений для очистки нет.")

    # Итог
    print()
    print("=" * 60)
    print("Итог:")
    print(
        f"  Оценено: {len(verdicts)} "
        f"(рекомендовано OK: "
        f"{sum(1 for v in verdicts if decide_verdict(v.percent) == VERDICT_OK)}, "
        f"WA: "
        f"{sum(1 for v in verdicts if decide_verdict(v.percent) == VERDICT_WA)})"
    )
    print(f"  OK (подтверждено): {len(ok_verdicts)}")
    print(f"  WrongAnswer (подтверждено): {len(wa_verdicts)}")
    print(f"  Ручная проверка: {len(manual)}")
    if skipped:
        print(f"  Пропущено (выход из интерактивного режима): {len(skipped)}")
    cancelled = [f for f in failures if CANCEL_REASON in f.reason]
    real_failures = [f for f in failures if CANCEL_REASON not in f.reason]
    if cancelled:
        print(f"  Отменено (Ctrl+C): {len(cancelled)}")
    if real_failures:
        print(f"  Ошибки: {len(real_failures)}")
        for failure in real_failures:
            where = failure.path or "-"
            print(
                f"    - {failure.alias} / {failure.submission_id if failure.submission_id is not None else '-'} "
                f"({where}): {failure.reason}"
            )

    if skipped:
        # Прервали интерактивный режим: необработанные файлы сохранены
        sys.exit(2)


def print_failures(failures: list[Failure]) -> None:
    for failure in failures:
        where = failure.path or "-"
        print(
            f"  - {failure.alias} / {failure.submission_id if failure.submission_id is not None else '-'} "
            f"({where}): {failure.reason}"
        )


if __name__ == "__main__":
    asyncio.run(main())
