# Yandex Contest Manager

Скрипты для работы с Crash-отправлениями в [Яндекс.Контесте](https://contest.yandex.ru): массовое игнорирование, скачивание исходников и автоматическое ревью через LLM.

## Требования

- Python 3.12+
- [aiohttp](https://pypi.org/project/aiohttp/)
- OAuth-токен Яндекс.Контеста

## Установка

```bash
git clone <repo-url> && cd ya-contest-manager
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

Токен Яндекс.Контеста выдаётся в [Яндекс.OAuth](https://oauth.yandex.ru/).

### Почему нужно вставлять JavaScript в консоль

Публичный API **не позволяет** менять вердикт посылки — только через админ-панель в браузере. Поэтому скрипты генерируют JavaScript для вставки в DevTools-консоль.

---

## Запуск

```bash
# Массовое игнорирование старых Crash (кроме последней посылки)
python3 make_ignored.py CONTEST_ID

# Просмотр / скачивание последних Crash-посылок
python3 crash_viewer.py CONTEST_ID
python3 crash_viewer.py CONTEST_ID --save --problem A --hours 3

# Скачивание всех задач в структуру tasks/<alias>/solutions/
./download_contest.sh CONTEST_ID

# Автоматическое ревью решений через LLM
python3 review_contest.py CONTEST_ID
```

Подробности опций — `python3 <script> --help`. Полная документация для разработчика — [`AGENTS.md`](AGENTS.md).

## Типичный рабочий процесс

```bash
./download_contest.sh 1426        # 1. Скачать Crash-посылки
vim tasks/A/statement.md           # 2. Заполнить условия задач
python3 review_contest.py 1426    # 3. Запустить ревью
                                   # 4. Вставить JS в консоль админ-панели
```

## Лицензия

[MIT](LICENSE) &copy; Eridan Domoratskiy
