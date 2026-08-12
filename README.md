# Yandex.Contest Manager

Скрипты для работы с Crash-отправлениями в [Яндекс.Контесте](https://contest.yandex.ru):
массовое игнорирование, скачивание исходников и автоматическое ревью через LLM.

## Требования

- Python 3.12+
- [aiohttp](https://pypi.org/project/aiohttp/)
- OAuth-токен Яндекс.Контеста

## Установка

```bash
git clone https://github.com/ProgMiner/ya-contest-manager && cd ya-contest-manager
python3 -m venv venv && source venv/bin/activate
pip install aiohttp
```

## Настройка

| Переменная | Обязательна | Описание |
|---|---|---|
| `YANDEX_CONTEST_TOKEN` | Да | OAuth-токен для API Яндекс.Контеста |
| `REVIEW_API_KEY` | Да* | Ключ для OpenAI-совместимого API (`review_contest.py`) |
| `TASKS_DIR` | Нет | Корневой каталог для `download_contest.sh` (по умолчанию `./tasks`) |

\* *Только для `review_contest.py`.*

```bash
export YANDEX_CONTEST_TOKEN="your-oauth-token"
export REVIEW_API_KEY="your-api-key"   # только для review_contest.py
```

> [!TIP]
> Для удобства создайте файл `.env` и загружайте его через `source .env` — он уже в `.gitignore`.

Для работы скриптов нужен OAuth-токен с правами **Управление соревнованиями и участниками** (`contest:manage`).
Порядок действий:
1. [Создайте приложение](https://oauth.yandex.ru/client/new/) в Яндекс.OAuth и выберите нужные права — см. [Доступ к API Контеста](https://admin.contest.yandex.ru/docs/ru/api-access).
2. [Получите токен вручную](https://yandex.ru/dev/id/doc/ru/tokens/debug-token) по `client_id` приложения.

### Почему нужно вставлять JavaScript в консоль

Публичный API **не позволяет** менять вердикт посылки — только через админ-панель в браузере.
Поэтому скрипты генерируют JavaScript для вставки в DevTools-консоль.

---

## Запуск

```bash
# Массовое игнорирование старых Crash (кроме последней посылки)
./make_ignored.py CONTEST_ID

# Просмотр / скачивание последних Crash-посылок
./crash_viewer.py CONTEST_ID
./crash_viewer.py CONTEST_ID --save --problem A --hours 3

# Скачивание всех задач в структуру tasks/<alias>/solutions/
./download_contest.sh CONTEST_ID

# Автоматическое ревью решений через LLM
./review_contest.py CONTEST_ID
```

Подробности опций — `./<script> --help`.
Полная документация для разработчика — [`AGENTS.md`](AGENTS.md).

## Типичный рабочий процесс

```bash
./download_contest.sh 1426  # 1. Скачать Crash-посылки
vim tasks/A/statement.md    # 2. Заполнить условия задач
./review_contest.py 1426    # 3. Запустить ревью
                            # 4. Вставить JS в консоль админ-панели
```
