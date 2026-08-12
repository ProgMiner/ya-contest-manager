#!/usr/bin/env python3

"""
Массовое игнорирование старых Crash-посылок в Яндекс.Контесте.

Для каждого (пользователь, задача) помечает все Crash-посылки
как Ignored, кроме самой последней по времени отправки.

Генерирует JavaScript для вставки в консоль админ-панели,
т.к. публичный API не позволяет менять вердикт.
"""

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from typing import Any

from api import (
    YandexContestAPI,
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

# Новый вердикт для старых посылок
IGNORED_VERDICT = "Ignored"


async def process_contest(
    contest_id: int,
    concurrency: int = CONCURRENCY,
    delay_ms: int = 500,
):
    async with YandexContestAPI(
        TOKEN,
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

        submissions = await api.get_all_submissions(contest_id)

        print(f"Всего посылок: {len(submissions)}")

        # Только Crash. Сравниваем регистронезависимо:
        # API может вернуть "CRASH", "Crash" и т.п.
        # Оригинальное значение вердикта сохраняем для логов.
        crashes = [
            s
            for s in submissions
            if (get_verdict(s) or "").lower() == CRASH_VERDICT
        ]

        print(f"Crash-посылок: {len(crashes)}")

        if crashes:
            sample_verdicts = sorted(
                v
                for v in {get_verdict(s) for s in crashes}
                if v is not None
            )
            print(
                "Оригинальные значения вердикта Crash: "
                f"{sample_verdicts}"
            )

        # ---------------------------------------------------------
        # Группируем:
        #
        #     один пользователь (authorId) + одна задача (problemId)
        #
        # Например:
        #
        # (123, "A") -> [submission 10, 15, 19]
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

        # ---------------------------------------------------------
        # Определяем, что именно нужно пометить Ignored
        #
        # ВАЖНО (требование задачи):
        # Если самых последних отправлений более одного
        # (равное submissionTime), то они ВСЕ считаются
        # самыми последними — выводим предупреждение с их ID.
        # ---------------------------------------------------------

        to_ignore: list[dict[str, Any]] = []

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
            latest = [
                x
                for x in group
                if parse_iso_time(get_submission_time(x)) == max_time
            ]

            if len(latest) > 1:
                print(
                    f"ВНИМАНИЕ: user={user_id} ({user_name}), "
                    f"problem={problem_id}: несколько самых последних "
                    f"Crash-посылок с одинаковым submissionTime "
                    f"({get_submission_time(latest[0])}): "
                    f"#{', #'.join(str(get_submission_id(x)) for x in latest)}"
                )

            latest_ids = {get_submission_id(x) for x in latest}

            # Всё, что НЕ входит в «самые последние», -> Ignored
            old_submissions = [
                x
                for x in group
                if get_submission_id(x) not in latest_ids
            ]

            if len(group) > 1:
                print(
                    f"user={user_id} ({user_name}), "
                    f"problem={problem_id}: "
                    f"{len(group)} Crash, "
                    f"оставляем {[get_submission_id(x) for x in latest]}, "
                    f"Ignored: "
                    f"{[get_submission_id(x) for x in old_submissions]}"
                )

            to_ignore.extend(old_submissions)

        print(
            f"Всего посылок будет переведено в Ignored: "
            f"{len(to_ignore)}"
        )

        if not to_ignore:
            print("Нечего менять.")
            return

        # ---------------------------------------------------------
        # Генерируем JavaScript для вставки в консоль
        # админ-панели Яндекс.Контеста
        #
        # Админ-панель использует внутренний API:
        #   PATCH /api/admin/contest/{contestId}/submission/verdict
        #        ?verdict=Ignored&filter=runId%3D{submissionId}
        #
        # с CSRF-токеном из текущей сессии браузера.
        # Выполнить этот запрос из скрипта напрямую нельзя —
        # нужен токен сессии. Поэтому генерируем JS-код,
        # который пользователь вставит в DevTools-консоль
        # на странице admin.contest.yandex.ru.
        # ---------------------------------------------------------

        submission_ids = [get_submission_id(s) for s in to_ignore]

        js = generate_admin_js(contest_id, submission_ids, IGNORED_VERDICT, delay_ms)

        print()
        print("=" * 60)
        print("JavaScript для вставки в консоль админ-панели:")
        print("=" * 60)
        print(js)
        print("=" * 60)
        print()
        print(
            "Инструкция:\n"
            "1. Открой https://admin.contest.yandex.ru\n"
            "2. Перейди к контесту → Посылки\n"
            "3. Открой DevTools (F12) → вкладка Console\n"
            "4. Вставь код выше и нажми Enter"
        )


def generate_admin_js(
    contest_id: int,
    submission_ids: list[int],
    verdict: str,
    delay_ms: int = 500,
) -> str:
    """
    Сгенерировать JavaScript для установки вердикта через
    админ-панель Яндекс.Контеста.

    Скрипт выполняется в контексте браузера на странице
    admin.contest.yandex.ru. CSRF-токен извлекается
    автоматически из мета-тега страницы или cookie.

    Параметры:
    - contest_id:    ID контеста
    - submission_ids: список runId посылок
    - verdict:       вердикт для установки ("Ignored")
    - delay_ms:      задержка между запросами (мс)
    """

    ids_json = json.dumps(submission_ids)

    return f"""\
(async () => {{
  const contestId = {contest_id};
  const verdict = {json.dumps(verdict)};
  const runIds = {ids_json};
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

  console.log(`Начинаю установку вердикта ${{verdict}} для ${{runIds.length}} посылок...`);
  console.log(`CSRF-токен: ${{csrfToken.substring(0, 8)}}...`);

  let ok = 0;
  let fail = 0;

  for (const runId of runIds) {{
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
    if (runId !== runIds[runIds.length - 1]) {{
      await new Promise(r => setTimeout(r, delayMs));
    }}
  }}

  console.log(`\\nГотово: ${{ok}} успешно, ${{fail}} с ошибкой из ${{runIds.length}}`);
}})();"""


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Пометить все Crash-посылки контеста как Ignored, "
            "кроме самой последней по времени отправки "
            "(для каждого пользователя и задачи)."
        ),
    )

    parser.add_argument(
        "contest_id",
        type=int,
        help="ID контеста",
    )

    parser.add_argument(
        "--delay",
        type=int,
        default=500,
        help=(
            "Задержка между запросами в JS (мс). "
            "По умолчанию 500."
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
            delay_ms=args.delay,
        )
    )


if __name__ == "__main__":
    main()
