#!/usr/bin/env python3

"""
Просмотр и скачивание Crash-посылок Яндекс.Контеста.

Фильтр, противоположный make_ignored.py:
- для каждого (пользователь, задача) оставляется ТОЛЬКО самая
  последняя Crash-посылка (все старые отбрасываются);
- пользователи, у которых есть ЛЮБАЯ посылка (не только Crash)
  за последние N часов (они ещё участвуют в контесте),
  исключаются целиком;
- для оставшихся посылок скачивается исходный код.

Два режима работы:
- без --save: выводит исходники в stdout, сгруппированные по задачам;
- с --save: сохраняет файлы в текущий каталог как <submission_id>.

Используемые эндпоинты публичного API v2:
- /contests/{contestId}/submissions
- /contests/{contestId}/submissions/{submissionId}/source
"""

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from api import (
    YandexContestAPI,
    BASE_URL,
    TOKEN,
    CONCURRENCY,
    CRASH_VERDICT,
    get_submission_id,
    get_user_id,
    get_user_name,
    get_problem_id,
    get_verdict,
    get_submission_time,
    parse_iso_time,
    sort_key,
)


async def fetch_source(
    api: YandexContestAPI,
    contest_id: int,
    submission_id: int,
    max_retries: int = 3,
    retry_delay: float = 1.0,
) -> bytes | None:
    """
    Получить исходный код посылки как сырые байты.

    Эндпоинт возвращает application/octet-stream (сырые байты),
    а НЕ JSON, поэтому обычный api.request() не подойдёт — он
    вызывает response.json(). Делаем прямой запрос через
    api.session с тем же api.semaphore для ограничения
    конкурентности.

    Возвращает сырые байты исходника либо None, если исходник
    недоступен (HTTP-ошибка).

    Примечание: бинарные/файловые посылки (с нулевыми байтами
    или невалидным UTF-8) НЕ отфильтровываются здесь — вызывающий
    код решает сам, что с ними делать.
    """

    assert api.session is not None

    # Как в YandexContestAPI.request(): убираем ведущий "/",
    # иначе путь «сбросит» префикс base_url при склейке.
    path = f"contests/{contest_id}/submissions/{submission_id}/source"

    data: bytes | None = None
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        async with api.semaphore:
            async with api.session.request(
                "GET", path,
                headers={"Accept": "*/*"},
            ) as response:

                if response.status >= 500:
                    # Серверная ошибка — retry с экспоненциальной
                    # задержкой, если есть ещё попытки
                    text = await response.text()
                    last_error = RuntimeError(
                        f"GET {path}: HTTP {response.status}: {text}"
                    )
                    if attempt < max_retries:
                        delay = retry_delay * (2 ** attempt)
                        print(
                            f"Предупреждение: GET {path} — "
                            f"HTTP {response.status}, повторная попытка "
                            f"{attempt + 1}/{max_retries} "
                            f"через {delay:.1f}с..."
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise last_error

                if response.status >= 400:
                    text = await response.text()
                    print(
                        f"Предупреждение: GET {path} — "
                        f"HTTP {response.status}: {text[:200]}"
                    )
                    return None

                # Ответ — сырые байты (application/octet-stream)
                data = await response.read()
                break

    # Эта строка достигается только через break после успешного ответа.
    # data=b"" — пустой, но валидный исходник.
    return data


def _is_binary(data: bytes) -> bool:
    """
    Эвристика для определения бинарных данных.

    Файловые посылки (загруженный архив, картинка и т.п.)
    содержат нулевые байты или не могут быть декодированы как UTF-8.
    """
    if b"\x00" in data:
        return True

    try:
        data.decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


def find_active_users(
    submissions: list[dict[str, Any]],
    hours: float,
) -> set[int]:
    """
    Пользователи, у которых есть ЛЮБАЯ посылка (не только Crash)
    за последние `hours` часов от текущего момента.

    Считаем, что такие пользователи всё ещё участвуют в контесте,
    поэтому все их Crash-посылки исключаются из просмотра.
    """

    threshold = datetime.now(timezone.utc).timestamp() - hours * 3600

    active_users: set[int] = set()

    for submission in submissions:
        try:
            user_id = get_user_id(submission)
        except ValueError:
            # Посылка без корректного authorId — на фильтр не влияет
            continue

        if parse_iso_time(get_submission_time(submission)) >= threshold:
            active_users.add(user_id)

    return active_users


def format_problem_header(submission: dict[str, Any]) -> str:
    """
    Заголовок блока задачи в выводе.

    Показываем alias задачи (если есть) и problemId —
    как в примере: "=== Задача A (problemId=123) ===".
    """

    problem_id = get_problem_id(submission)

    alias = submission.get("problemAlias")

    return f"=== Задача {alias or problem_id} (problemId={problem_id}) ==="


async def process_contest(
    contest_id: int,
    concurrency: int = CONCURRENCY,
    base_url: str | None = None,
    hours: float = 2.5,
    problem_filter: list[str] | None = None,
    save: bool = False,
    dry_run: bool = False,
):
    async with YandexContestAPI(
        TOKEN,
        base_url=base_url or BASE_URL,
        concurrency=concurrency,
    ) as api:

        print(f"Получаю посылки контеста {contest_id}...")

        # ---------------------------------------------------------
        # Заголовок: имя контеста (если получится получить)
        # ---------------------------------------------------------

        contest_info = await api.get_contest_info(contest_id)

        if contest_info:
            name = (
                contest_info.get("name")
                or contest_info.get("title")
                or contest_id
            )
            print(f"Контест: {name} (id={contest_id})")
        else:
            print(f"Контест: id={contest_id}")

        # ---------------------------------------------------------
        # 1. Все посылки контеста
        # ---------------------------------------------------------

        submissions = await api.get_all_submissions(contest_id)

        print(f"Всего посылок: {len(submissions)}")

        # ---------------------------------------------------------
        # 2. Исключаем пользователей, которые ещё участвуют:
        #    у них есть ЛЮБАЯ посылка за последние `hours` часов
        # ---------------------------------------------------------

        active_users = find_active_users(submissions, hours)

        if active_users:
            print(
                f"Активных пользователей (посылка за последние "
                f"{hours:g} ч): {len(active_users)}"
            )

        inactive = []
        for s in submissions:
            try:
                if get_user_id(s) not in active_users:
                    inactive.append(s)
            except ValueError:
                # Посылка без корректного authorId — на фильтр не влияет
                continue

        print(
            f"После фильтра активных пользователей: {len(inactive)}"
        )

        # ---------------------------------------------------------
        # 3. Только Crash. Сравниваем регистронезависимо:
        #    API может вернуть "CRASH", "Crash" и т.п.
        # ---------------------------------------------------------

        crashes = [
            s
            for s in inactive
            if (get_verdict(s) or "").lower() == CRASH_VERDICT
        ]

        print(f"Crash-посылок: {len(crashes)}")

        if not crashes:
            print("Нечего показывать.")
            return

        # ---------------------------------------------------------
        # 4. Группируем по (пользователь, задача):
        #
        #     (authorId, problemId) -> [посылки]
        #
        # и в каждой группе оставляем ТОЛЬКО самую последнюю
        # Crash-посылку — противоположность make_ignored.py,
        # где последняя сохранялась, а все старые шли в Ignored.
        # ---------------------------------------------------------

        groups: dict[tuple[int, str], list[dict[str, Any]]] = (
            defaultdict(list)
        )

        for submission in crashes:
            try:
                user_id = get_user_id(submission)
            except ValueError:
                print(
                    f"ВНИМАНИЕ: пропуск посылки без корректного "
                    f"authorId: {submission}"
                )
                continue
            key = (
                user_id,
                get_problem_id(submission),
            )
            groups[key].append(submission)

        latest: list[dict[str, Any]] = []

        for key, group in groups.items():
            user_id, problem_id = key
            user_name = get_user_name(group[0]) or "?"

            # От старой к новой: по времени отправки (ID — tiebreaker)
            group.sort(key=sort_key)

            # Максимальное время отправки в группе
            max_time = max(
                parse_iso_time(get_submission_time(x))
                for x in group
            )

            # ВСЕ посылки с максимальным временем — «самые последние»
            latest_in_group = [
                x
                for x in group
                if parse_iso_time(get_submission_time(x)) == max_time
            ]

            if len(latest_in_group) > 1:
                print(
                    f"ВНИМАНИЕ: user={user_id} ({user_name}), "
                    f"problem={problem_id}: несколько последних "
                    f"Crash-посылок с одинаковым submissionTime "
                    f"({get_submission_time(latest_in_group[0])}): "
                    f"#{', #'.join(str(get_submission_id(x)) for x in latest_in_group)}"
                )

            latest.extend(latest_in_group)

        print(
            f"После фильтра «последняя Crash» "
            f"(по пользователю и задаче): {len(latest)}"
        )

        remaining = latest

        if not remaining:
            print("Нечего показывать.")
            return

        # ---------------------------------------------------------
        # 5. Опциональный фильтр по задачам (--problem)
        #
        # Поддерживаются два варианта:
        # - числовой ID задачи: --problem 12345
        # - буквенный alias:    --problem A --problem B
        #
        # Различаем по содержимому: если значение состоит только
        # из цифр — это problemId, иначе — problemAlias.
        # ---------------------------------------------------------

        if problem_filter:
            allowed_ids: set[str] = set()
            allowed_aliases: set[str] = set()

            for value in problem_filter:
                if value.isdigit():
                    allowed_ids.add(value)
                else:
                    allowed_aliases.add(value)

            remaining = [
                s
                for s in remaining
                if get_problem_id(s) in allowed_ids
                or (s.get("problemAlias") or "") in allowed_aliases
            ]

            parts: list[str] = []
            if allowed_ids:
                parts.append(
                    f"id: {', '.join(sorted(allowed_ids))}"
                )
            if allowed_aliases:
                parts.append(
                    f"alias: {', '.join(sorted(allowed_aliases))}"
                )
            print(
                f"После фильтра по задачам "
                f"({'; '.join(parts)}): "
                f"{len(remaining)}"
            )
            if not remaining:
                print("Нечего показывать.")
                return

        # ---------------------------------------------------------
        # 6. Выводим список посылок по задачам
        # ---------------------------------------------------------

        by_problem: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for s in remaining:
            by_problem[get_problem_id(s)].append(s)

        for problem_id in sorted(by_problem):
            items = by_problem[problem_id]
            alias = items[0].get("problemAlias") or problem_id
            print(
                f"Задача {alias} (problemId={problem_id}): "
                f"{len(items)} посылок"
            )
            for s in items:
                user_name = get_user_name(s) or "?"
                user_id = get_user_id(s)
                compiler = s.get("compiler", "")
                print(f"  {user_name} (id={user_id}, compiler={compiler!r})")

        if dry_run:
            print("\n(режим dry-run, исходники не загружаются)")
            return

        # ---------------------------------------------------------
        # 7. Загружаем исходники
        # ---------------------------------------------------------

        print(f"\nЗагружаю исходники {len(remaining)} посылок...")

        # Параллельная загрузка (ограничение конкурентности
        # через Semaphore внутри fetch_source)
        async def _fetch_one(
            submission: dict[str, Any],
        ) -> tuple[dict[str, Any], bytes | None]:
            source = await fetch_source(
                api, contest_id, get_submission_id(submission),
            )
            return submission, source

        results = await asyncio.gather(
            *[_fetch_one(s) for s in remaining],
            return_exceptions=True,
        )

        # Группируем по задачам
        by_problem_data: dict[str, list[tuple[dict[str, Any], bytes]]] = (
            defaultdict(list)
        )
        sources_ok = 0
        skipped_binary = 0
        skipped_error = 0

        for result in results:
            if isinstance(result, BaseException):
                skipped_error += 1
                print(
                    f"  ПРОПУСК: ошибка при загрузке исходника: {result}",
                    file=sys.stderr,
                )
                continue
            submission, source = result

            try:
                submission_id = get_submission_id(submission)
                user_id = get_user_id(submission)
                problem_id = get_problem_id(submission)
            except ValueError as exc:
                skipped_error += 1
                print(
                    f"  ПРОПУСК: некорректные данные посылки: {exc}",
                    file=sys.stderr,
                )
                continue

            if source is None:
                skipped_error += 1
                print(
                    f"  ПРОПУСК user={user_id}: "
                    f"исходник недоступен (посылка #{submission_id})"
                )
                continue

            if _is_binary(source):
                skipped_binary += 1
                print(
                    f"  ПРОПУСК user={user_id}: "
                    f"бинарный файл (посылка #{submission_id})"
                )
                continue

            sources_ok += 1
            by_problem_data[problem_id].append(
                (submission, source)
            )

        print(f"Успешно получено исходников: {sources_ok}")

        if not by_problem_data:
            print("Нечего показывать.")
            return

        # ---------------------------------------------------------
        # 8. Вывод / сохранение
        # ---------------------------------------------------------

        if save:
            # Сохраняем в текущий каталог как <submission_id>
            # (submission_id = runId в админ-панели — имя файла
            # совпадает с ID посылки)
            saved = 0

            for problem_id in sorted(by_problem_data):
                items = by_problem_data[problem_id]

                for submission, source in items:
                    submission_id = get_submission_id(submission)
                    filepath = str(submission_id)

                    with open(filepath, "wb") as f:
                        f.write(source)

                    saved += 1
                    print(
                        f"  СОХРАНЕНО {submission_id} ({len(source)} байт)"
                    )

            print(
                f"\nИтого: сохранено {saved}, "
                f"пропущено (бинарные): {skipped_binary}, "
                f"пропущено (ошибки): {skipped_error}"
            )

        else:
            # Выводим в stdout, сгруппированный по задачам
            for problem_id in sorted(by_problem_data):
                items = by_problem_data[problem_id]

                # Внутри задачи сортируем по времени отправки
                items.sort(key=lambda pair: sort_key(pair[0]))

                print()
                print(format_problem_header(items[0][0]))
                print()

                for submission, source in items:
                    user_name = get_user_name(submission) or "?"
                    user_id = get_user_id(submission)
                    submission_id = get_submission_id(submission)
                    submission_time = get_submission_time(submission) or "?"

                    print(
                        f"--- Пользователь: {user_name} (id={user_id}), "
                        f"посылка #{submission_id}, "
                        f"время: {submission_time} ---"
                    )
                    print(source.decode("utf-8", errors="replace"))
                    print()


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Просмотр и скачивание последних Crash-посылок контеста: "
            "для каждого (пользователь, задача) оставляется только "
            "самая последняя Crash-посылка, а пользователи "
            "с активностью за последние N часов исключаются."
        ),
    )

    parser.add_argument(
        "contest_id",
        type=int,
        help="ID контеста",
    )

    parser.add_argument(
        "--problem",
        action="append",
        default=[],
        help=(
            "Задача для показа: числовой ID (например 12345) "
            "или буквенный alias (например A); можно указывать "
            "несколько раз; если не указан — показываются все задачи"
        ),
    )

    parser.add_argument(
        "--concurrency",
        type=int,
        default=CONCURRENCY,
        help=(
            "Максимальное количество одновременных HTTP-запросов "
            f"(по умолчанию {CONCURRENCY})"
        ),
    )

    parser.add_argument(
        "--hours",
        type=float,
        default=2.5,
        help=(
            "Порог «активности» пользователя в часах: если у "
            "пользователя есть ЛЮБАЯ посылка за последние N часов, "
            "он считается всё ещё участвующим и исключается "
            "(по умолчанию 2.5)"
        ),
    )

    parser.add_argument(
        "--save",
        action="store_true",
        default=False,
        help=(
            "Сохранить исходники в текущий каталог как <submission_id> "
            "вместо вывода в stdout"
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Показать список посылок, но не загружать исходники",
    )

    args = parser.parse_args()

    if not TOKEN:
        print(
            "Ошибка: не задана переменная окружения "
            "YANDEX_CONTEST_TOKEN",
            file=sys.stderr,
        )
        sys.exit(1)

    asyncio.run(
        process_contest(
            args.contest_id,
            concurrency=args.concurrency,
            hours=args.hours,
            problem_filter=args.problem,
            save=args.save,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()
