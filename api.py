#!/usr/bin/env python3

"""
Общий модуль для работы с публичным API Яндекс.Контеста v2.

Содержит:
- YandexContestAPI — класс для работы с API (auth, request с retry, pagination)
- Константы: BASE_URL, TOKEN, CONCURRENCY, CRASH_VERDICT
- Хелперы для извлечения полей из JSON посылки
- parse_iso_time, sort_key — для сортировки и сравнения времени
"""

import asyncio
import os
from datetime import datetime, timezone
from typing import Any

import aiohttp


# Базовый URL публичного API Яндекс.Контеста.
# Можно переопределить через окружение YANDEX_CONTEST_BASE_URL.
BASE_URL = os.environ.get(
    "YANDEX_CONTEST_BASE_URL",
    "https://api.contest.yandex.net/api/public/v2",
)

# Можно передать через окружение:
# export YANDEX_CONTEST_TOKEN="..."
TOKEN = os.environ.get("YANDEX_CONTEST_TOKEN", "")

# Максимальное количество одновременных HTTP-запросов
CONCURRENCY = 10

# Вердикт, который считается "плохим".
# Сравнение регистронезависимое — API может вернуть
# "CRASH", "Crash", "crash" и т.п.
CRASH_VERDICT = "crash"


class YandexContestAPI:
    def __init__(
        self,
        token: str,
        base_url: str = BASE_URL,
        concurrency: int = 10,
    ):
        self.token = token
        # aiohttp требует trailing slash в base_url, иначе путь
        # не склеивается:
        #   base_url "https://host/api/v2" + "/contests/..." ->
        #   "https://host/contests/..."  (потерянный префикс!)
        self.base_url = base_url.rstrip("/") + "/"
        self.semaphore = asyncio.Semaphore(concurrency)

        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            base_url=self.base_url,
            headers={
                "Authorization": f"OAuth {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def request(
        self,
        method: str,
        path: str,
        *,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        **kwargs,
    ) -> Any:
        assert self.session is not None

        # Пути внутри кода начинаются с "/", но aiohttp при
        # склейке с base_url ведёт себя как urljoin: абсолютный
        # путь "сбрасывает" префикс base_url. Убираем ведущий "/",
        # чтобы путь склеивался с базой:
        #   base "https://host/api/public/v2/" + "contests/..." ->
        #   "https://host/api/public/v2/contests/..."
        path = path.lstrip("/")

        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            async with self.semaphore:
                async with self.session.request(
                    method,
                    path,
                    **kwargs,
                ) as response:

                    if response.status >= 500:
                        # Серверная ошибка — retry с экспоненциальной
                        # задержкой, если есть ещё попытки
                        text = await response.text()
                        last_error = RuntimeError(
                            f"{method} {path}: "
                            f"HTTP {response.status}: {text}"
                        )
                        if attempt < max_retries:
                            delay = retry_delay * (2 ** attempt)
                            print(
                                f"Предупреждение: {method} {path} — "
                                f"HTTP {response.status}, "
                                f"повторная попытка {attempt + 1}/"
                                f"{max_retries} через {delay:.1f}с..."
                            )
                            await asyncio.sleep(delay)
                            continue
                        raise last_error

                    if response.status >= 400:
                        text = await response.text()
                        raise RuntimeError(
                            f"{method} {path}: "
                            f"HTTP {response.status}: {text}"
                        )

                    if response.status == 204:
                        return None

                    return await response.json()

    async def get_contest_info(
        self,
        contest_id: int,
    ) -> dict[str, Any] | None:
        """
        Получить информацию о контесте (имя и т.п.) для заголовка.

        Если эндпоинт недоступен — не прерываем работу,
        возвращаем None.
        """

        try:
            data = await self.request(
                "GET",
                f"/contests/{contest_id}",
            )
            if isinstance(data, dict):
                return data
        except Exception as exc:  # noqa: BLE001
            print(
                f"Предупреждение: не удалось получить "
                f"информацию о контесте {contest_id}: {exc}"
            )

        return None

    async def get_all_submissions(
        self,
        contest_id: int,
    ) -> list[dict[str, Any]]:
        """
        Получить все посылки контеста.

        Основной формат ответа API v2 (см. спецификацию):

            {
              "count": 123,
              "submissions": [...]
            }

        Поддерживаются и fallback-варианты:
        {"items": [...]}, {"content": [...]} либо просто [...] —
        на случай нестандартных версий API.
        """

        result = []

        page = 1
        page_size = 100

        while True:
            data = await self.request(
                "GET",
                f"/contests/{contest_id}/submissions",
                params={
                    "page": page,
                    "pageSize": page_size,
                },
            )

            # Основной формат: {"count": ..., "submissions": [...]}
            if isinstance(data, dict):
                items = []
                for key in ("submissions", "items", "content"):
                    if key in data:
                        items = data[key]
                        break
                count = data.get("count")
            else:
                # Fallback: API вернул просто список посылок
                items = data or []
                count = None

            if not items:
                break

            result.extend(items)

            # Останавливаемся, если:
            # - собрали всё по счётчику count (основной формат API v2)
            # - счётчика нет, а последняя страница оказалась неполной
            #   (fallback для вариантов ответа без count)
            if count is not None:
                if len(result) >= int(count):
                    break
            elif len(items) < page_size:
                break

            page += 1

        return result


# ----------------------------------------------------------------------
# Вспомогательные функции извлечения полей из JSON посылки
#
# По спецификации API v2 Submission — плоская модель:
#   id, authorId, author, problemId, problemAlias, verdict,
#   submissionTime, compiler, memory, time, test, score, ...
#
# Fallback-варианты сохранены на случай нестандартных версий API.
# ----------------------------------------------------------------------


def get_submission_id(s: dict[str, Any]) -> int:
    value = None
    for key in ("id", "submissionId"):
        v = s.get(key)
        if v is not None:
            value = v
            break

    if value is None:
        raise ValueError(
            f"Не удалось определить submission ID: {s}"
        )

    return int(value)


def get_user_id(s: dict[str, Any]) -> int:
    """
    ID участника.

    Основное поле по спецификации API v2 — authorId (int64).
    """

    value = None
    for key in ("authorId", "participantId", "userId"):
        v = s.get(key)
        if v is not None:
            value = v
            break

    if value is None:
        participant = s.get("participant")
        if isinstance(participant, dict):
            value = None
            for key in ("id", "participantId", "userId"):
                v = participant.get(key)
                if v is not None:
                    value = v
                    break

    if value is None:
        raise ValueError(
            f"Не удалось определить user/participant ID: {s}"
        )

    return int(value)


def get_user_name(s: dict[str, Any]) -> str | None:
    """
    Имя участника.

    Основное поле по спецификации API v2 — author (string).
    """

    author = s.get("author")

    if author is None:
        participant = s.get("participant")
        if isinstance(participant, dict):
            author = participant.get("name")

    return author if author is not None else None


def get_problem_id(s: dict[str, Any]) -> str:
    """
    ID задачи.

    ВАЖНО: по спецификации API v2 problemId — строка (string),
    а не число! Возвращаем именно строку.
    """

    value = s.get("problemId")

    if value is None:
        problem = s.get("problem")
        if isinstance(problem, dict):
            value = None
            for key in ("id", "problemId"):
                v = problem.get(key)
                if v is not None:
                    value = v
                    break

    if value is None:
        raise ValueError(
            f"Не удалось определить problem ID: {s}"
        )

    return str(value)


def get_verdict(s: dict[str, Any]) -> str | None:
    """
    Вердикт посылки.

    По спецификации API v2 verdict — обычная строка
    (например, "CRASH", "OK", "WA"), а НЕ объект.
    Регистр может отличаться — сравнивать нужно
    case-insensitive, а для вывода сохранять оригинал.
    """

    verdict = s.get("verdict")

    if isinstance(verdict, dict):
        # Fallback для нестандартных версий, где verdict — объект
        return (
            verdict.get("name")
            or verdict.get("code")
            or verdict.get("value")
        )

    if verdict is None:
        return None

    return str(verdict)


def get_submission_time(s: dict[str, Any]) -> str | None:
    """
    Время отправки.

    Основное поле по спецификации API v2 — submissionTime
    (строка, ISO 8601).
    """

    return (
        s.get("submissionTime")
        or s.get("createdAt")
        or s.get("submittedAt")
        or s.get("time")
    )


def parse_iso_time(value: str | None) -> float:
    """
    Переводим ISO-строку времени в эпоху (секунды)
    для корректной сортировки и сравнения.
    Пустые/неразбираемые значения -> 0.0.
    """

    if not value:
        return 0.0

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # Наивное время считаем UTC, чтобы не ловить ошибки
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0.0


def sort_key(s: dict[str, Any]):
    """
    Критерий сортировки группы: "по времени отправки".

    Требование задачи — сортировать по submissionTime,
    а ID использовать как tiebreaker при равном времени.
    """

    return (
        parse_iso_time(get_submission_time(s)),
        get_submission_id(s),
    )
