# Инструкция по запуску

## Файлы проекта

```
telegram_bot/
├── bot.py              — основной файл бота
├── sheets.py           — работа с Google Таблицей
├── requirements.txt    — зависимости
├── .env.example        — шаблон конфига → скопируйте в .env
├── credentials.json    — ключ Google (создаёте сами, см. ниже)
└── SETUP.md            — эта инструкция
```

---

## Шаг 1 — Создать Telegram-бота

1. Напишите [@BotFather](https://t.me/BotFather)
2. `/newbot` → придумайте имя и username
3. Скопируйте токен вида `7123456789:AAHxxx...`

Узнайте свой **Telegram ID** (нужен для прав администратора):
1. Напишите [@userinfobot](https://t.me/userinfobot)
2. Скопируйте число из поля `Id:`, например `123456789`

---

## Шаг 2 — Google Таблица

### 2.1 Создать сервисный аккаунт

1. Откройте [Google Cloud Console](https://console.cloud.google.com/)
2. Создайте или выберите проект
3. **APIs & Services → Library** → найдите **Google Sheets API** → Enable
4. **APIs & Services → Credentials → Create Credentials → Service account**
5. Придумайте имя → Create → Done
6. Кликните на аккаунт → вкладка **Keys → Add Key → JSON**
7. Скачайте файл, переименуйте в `credentials.json`, положите рядом с `bot.py`

### 2.2 Создать таблицу

1. Откройте [Google Sheets](https://sheets.google.com/), создайте новую таблицу
2. Назовите нижнюю вкладку **Бронирования**
3. Добавьте заголовки в строку 1:

| A | B | C | D | E | F | G | H |
|---|---|---|---|---|---|---|---|
| Дата | Статус | Кол-во гостей | Имя клиента | Телефон | Источник рекламы | Прямой клиент / Агентство | Комментарий |

4. Заполните известные даты:

| 20.05.2026 | Занято | 80 | Иван Петров | +79001234567 | Авито | Прямой клиент | |
| 25.05.2026 | Свободно | | | | | | |

5. Скопируйте **ID таблицы** из URL:
   `https://docs.google.com/spreadsheets/d/`**`ВОТ_ЭТО_ID`**`/edit`

### 2.3 Дать доступ сервисному аккаунту

1. Откройте `credentials.json`, найдите поле `"client_email"` — скопируйте email
2. В Google Таблице нажмите **Поделиться**
3. Вставьте email → доступ **Редактор**

---

## Шаг 3 — Настроить .env

```bash
cp .env.example .env
```

Откройте `.env` и заполните все поля:
```
TELEGRAM_BOT_TOKEN=7123456789:AAHxxx...
ADMIN_CHAT_ID=123456789
SPREADSHEET_ID=1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms
SHEET_NAME=Бронирования
GOOGLE_CREDENTIALS_PATH=credentials.json
```

---

## Шаг 4 — Установить и запустить

```bash
pip install -r requirements.txt
python bot.py
```

---

## Шаг 5 — Запуск как сервис на VPS

Создайте файл `/etc/systemd/system/tgbot.service`:

```ini
[Unit]
Description=Telegram Booking Bot
After=network.target

[Service]
WorkingDirectory=/путь/к/telegram_bot
ExecStart=/usr/bin/python3 /путь/к/telegram_bot/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable tgbot
sudo systemctl start tgbot
sudo systemctl status tgbot
```

---

## Как пользоваться

### Автоматическая проверка дат

Бот следит за **всеми сообщениями в чате**. Как только кто-то напишет дату (в любом формате) — бот сразу ответит:

> «15 мая, хочу забронировать»
> ↓
> Бот: ✅ Дата 15.05.2026 (пт) свободна! Можем прислать презентацию.

Поддерживаемые форматы дат: `15 мая`, `15 мая 2026`, `15.05.2026`, `15.05`, `15/05/2026`

---

### Команды администратора

Работают только для пользователя с ADMIN_CHAT_ID.

| Команда | Что делает |
|---------|------------|
| `/add` | Добавить бронь в Google Таблицу (пошаговый диалог) |
| `/cancel_booking` | Снять бронь — дата станет «Свободно» |
| `/bookings` | Посмотреть все брони (предстоящие и прошедшие) |
| `/free` | Список свободных дат |
| `/stop` | Отменить текущий диалог |

### Пример добавления брони через /add

```
Вы: /add
Бот: На какую дату?
Вы: 20 июня
Бот: Сколько гостей?
Вы: 100
Бот: Имя клиента?
Вы: Анна Смирнова
Бот: Телефон?
Вы: +79001234567
Бот: Источник рекламы? [кнопки]
Вы: Авито
Бот: Прямой клиент или агентство? [кнопки]
Вы: Прямой клиент
Бот: Комментарий?
Вы: Свадьба, нужен выезд на природу
Бот: ✅ Бронь добавлена в таблицу!
```
