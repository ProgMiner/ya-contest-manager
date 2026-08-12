#!/usr/bin/env bash
#
# Скачать решения контеста в структуру каталогов tasks/<alias>/solutions/.
#
# Использование:
#   ./download_contest.sh CONTEST_ID [OPTIONS...]
#
# Примеры:
#   ./download_contest.sh 95164
#   ./download_contest.sh 95164 --problem A --problem B
#   ./download_contest.sh 95164 --hours 3
#   ./download_contest.sh 95164 --dry-run
#
# Структура:
#   tasks/
#     A/
#       statement.md        ← размещаешь вручную
#       solutions/
#         12345             ← файл без расширения, имя = ID посылки (submission_id = runId)
#         67890
#     B/
#       statement.md
#       solutions/
#         11111
#
# Скрипт делает следующее:
# 1. Запускает crash_viewer.py --dry-run чтобы узнать список задач
#    и посылок (без скачивания).
# 2. Для каждой задачи создаёт каталог tasks/<alias>/solutions/.
# 3. Запускает crash_viewer.py --save --problem <alias> в этом каталоге,
#    чтобы он сохранил файлы решений туда.
#
# Переменные окружения:
#   YANDEX_CONTEST_TOKEN  — OAuth-токен (обязателен)
#   TASKS_DIR             — корневой каталог (по умолчанию ./tasks)
#

set -euo pipefail

# ------------------------------------------------------------------
# Настройки
# ------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_SCRIPT="$SCRIPT_DIR/crash_viewer.py"
TASKS_DIR="${TASKS_DIR:-./tasks}"

# ------------------------------------------------------------------
# Проверки
# ------------------------------------------------------------------

if [ -z "${YANDEX_CONTEST_TOKEN:-}" ]; then
    echo "Ошибка: не задана переменная YANDEX_CONTEST_TOKEN" >&2
    echo "  export YANDEX_CONTEST_TOKEN=\"...\"" >&2
    exit 1
fi

if [ $# -lt 1 ]; then
    echo "Использование: $0 CONTEST_ID [OPTIONS...]" >&2
    echo "" >&2
    echo "OPTIONS передаются в crash_viewer.py:" >&2
    echo "  --problem A|B|...   фильтр по задачам (можно несколько)" >&2
    echo "  --hours N           порог активности (по умолчанию 2.5)" >&2
    echo "  --concurrency N     конкурентность API-запросов" >&2
    echo "  --dry-run           не сохранять файлы, только показать" >&2
    exit 1
fi

CONTEST_ID="$1"
shift

# ------------------------------------------------------------------
# Парсим --problem из аргументов, чтобы знать какие задачи
# скачивать. Если --problem не указан — нужно узнать список задач
# из API (через dry-run).
# ------------------------------------------------------------------

# Собираем --problem аргументы отдельно
PROBLEMS=()
OTHER_ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --problem)
            if [ $# -lt 2 ]; then
                echo "Ошибка: --problem требует значение" >&2
                exit 1
            fi
            PROBLEMS+=("$2")
            shift 2
            ;;
        *)
            OTHER_ARGS+=("$1")
            shift
            ;;
    esac
done

# ------------------------------------------------------------------
# Определяем список задач.
#
# Если --problem не указан, делаем dry-run чтобы узнать,
# какие задачи есть в контесте. Парсим строки вида:
#   "Задача A (problemId=123): 5 посылок"
# ------------------------------------------------------------------

if [ ${#PROBLEMS[@]} -eq 0 ]; then
    echo "Определяю список задач через dry-run..."
    DRY_OUTPUT=$(python3 "$PYTHON_SCRIPT" "$CONTEST_ID" \
        --dry-run "${OTHER_ARGS[@]+"${OTHER_ARGS[@]}"}" 2>&1) || {
        echo "Ошибка при dry-run:" >&2
        echo "$DRY_OUTPUT" >&2
        exit 1
    }

    # Парсим строки "Задача <alias> (problemId=...): N посылок"
    while IFS= read -r line; do
        if [[ "$line" =~ ^Задача[[:space:]]+([^[:space:]]+)[[:space:]]+\(problemId= ]]; then
            PROBLEMS+=("${BASH_REMATCH[1]}")
        fi
    done <<< "$DRY_OUTPUT"

    if [ ${#PROBLEMS[@]} -eq 0 ]; then
        echo "Задачи не найдены. Возможно, все пользователи активны или нет Crash-посылок."
        exit 0
    fi

    echo "Найдены задачи: ${PROBLEMS[*]}"
fi

# ------------------------------------------------------------------
# Создаём структуру каталогов и скачиваем
# ------------------------------------------------------------------

echo "Скачиваю в каталог: $TASKS_DIR"
echo ""

for alias in "${PROBLEMS[@]}"; do
    task_dir="$TASKS_DIR/$alias"
    solutions_dir="$task_dir/solutions"

    # Создаём каталог решений (и родительский)
    mkdir -p "$solutions_dir"

    # Создаём пустой statement.md если его нет
    if [ ! -f "$task_dir/statement.md" ]; then
        touch "$task_dir/statement.md"
        echo "Создан пустой $task_dir/statement.md — заполните условие задачи"
    fi

    # Скачиваем решения в каталог solutions/
    echo "========================================"
    echo "Задача $alias → $solutions_dir"
    echo "========================================"

    # Передаём --problem с alias, чтобы скачать только эту задачу.
    # crash_viewer.py --save сохраняет файлы как <submission_id>
    # в текущий каталог.
    (
        cd "$solutions_dir"
        python3 "$PYTHON_SCRIPT" "$CONTEST_ID" \
            --save \
            --problem "$alias" \
            "${OTHER_ARGS[@]+"${OTHER_ARGS[@]}"}"
    )

    echo ""
done

echo "========================================"
echo "Готово! Структура в $TASKS_DIR:"
echo ""

# Показать что получилось
for alias in "${PROBLEMS[@]}"; do
    solutions_dir="$TASKS_DIR/$alias/solutions"
    if [ -d "$solutions_dir" ]; then
        count=$(find "$solutions_dir" -type f | wc -l)
        echo "  $alias/ ($count файлов)"
        ls -1 "$solutions_dir" | sed 's/^/    /'
    fi
done
