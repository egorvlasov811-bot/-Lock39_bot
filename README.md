# 🔐 Камера хранения — Telegram Mini App

Бронирование ячеек в Telegram. Один процесс: бот + API + веб-приложение.

## Файлы проекта

```
storage-bot/
├── app.py              ← всё в одном (бот + сервер)
├── index.html          ← мини-приложение
├── requirements.txt    ← зависимости
├── Procfile            ← команда запуска для Railway
└── README.md
```

## Переменные окружения

| Имя | Описание |
|---|---|
| `BOT_TOKEN` | Токен от @BotFather |
| `WEBAPP_URL` | HTTPS-адрес твоего деплоя (Railway сам выдаёт) |
| `ADMIN_CHAT_ID` | Твой Telegram ID — куда слать уведомления |

## Запуск локально

```bash
pip install -r requirements.txt
export BOT_TOKEN="123:ABC..."
export WEBAPP_URL="https://your-ngrok.ngrok.io"
export ADMIN_CHAT_ID="123456789"
python app.py
```
