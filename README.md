# Telegram → Bluesky auto-publisher

Автоматически переносит публикации из Telegram-канала в аккаунт Bluesky.
Полностью бесплатно: без платных API, без внешних сервисов, без подписок.

## Возможности

- Приём новых сообщений канала через Telegram Bot API (long polling).
- Текст, фото, видео, текст+фото, текст+видео, альбомы (несколько фото/видео).
- Длинный текст (>300 символов) автоматически разбивается на цепочку (thread).
- Альбомы с >4 фото или видео разбиваются на несколько связанных постов.
- Очередь с повторными попытками, защитой от дублей и журналом (SQLite).
- Управление через команды боту (`/status`, `/queue`, `/errors`, `/retry`).
- Автозапуск через systemd, восстановление после перезагрузки VPS.

## Почему Bluesky, а не X (Twitter)

Официальный X API с 2024–2026 года работает только по модели pay-per-use
(нет бесплатного тарифа): создание поста — $0.015 (с ссылкой $0.20), загрузка
медиа — $0.005/файл. Надёжного бесплатного способа публикации в X не
существует; бесплатные «обходы» через cookies нарушают ToS и ведут к блокировке.

Bluesky (AT Protocol) предоставляет полностью бесплатный и легальный API
без тарифов и без ревью приложений, поэтому выбран как целевая платформа.

## Настройка

1. Создайте бота у @BotFather, получите токен.
2. Добавьте бота администратором в исходный канал (право «Публикация
   сообщений»). Получите ID канала (например, переслав сообщение канала
   боту @userinfobot).
3. Создайте App Password в Bluesky: Настройки → App passwords.
4. Скопируйте `.env.example` в `.env` и заполните:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_SOURCE_CHAT_ID=-100...
BSKY_HANDLE=you.bsky.social
BSKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
ADMIN_USER_IDS=ваш_telegram_id
```

## Запуск локально

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt   # или .venv\Scripts\pip на Windows
.venv/bin/python main.py
```

## Развёртывание на VPS

```
sudo mkdir -p /opt/telegram-bluesky
sudo cp -r . /opt/telegram-bluesky/
cd /opt/telegram-bluesky
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# заполнить .env
sudo cp deploy/telegram-bluesky.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-bluesky
```

Проверка: `sudo systemctl status telegram-bluesky` и
`journalctl -u telegram-bluesky -f`.

## Управление через бота

Напишите боту (в личку) команды:

- `/status` — сводка очереди
- `/queue` — ожидающие публикации
- `/errors` — последние ошибки
- `/retry` — повторно поставить в очередь неудачные
- `/recent` — последние опубликованные

Доступ ограничен `ADMIN_USER_IDS` из `.env`.

## Безопасность

- Секреты только в `.env` (добавлен в `.gitignore`).
- Токены и пароли не пишутся в логи.
- Управляющие команды доступны только указанным администраторам.
