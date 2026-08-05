# Восстановление бота «Муза» на новом сервере (Бегет)

Инструкция «с нуля» на случай потери сервера. Проверено на связке: Ubuntu 22.04 + Docker + системный nginx + Let's Encrypt.

---

## 0. Что уцелело, а что нужно восстановить

**Уцелело (не на сервере):**
- **Код** — на GitHub: `github.com/rimskiymedved/muza_bot`.
- **Данные (брони, лиды)** — в Google-таблице. Это источник правды; SQLite на сервере — лишь кэш, он пересоберётся из таблицы при первом запуске.

**Было только на сервере — восстановить из бэкапа или создать заново:**
- `.env` — секреты и настройки (токен бота, ключи Авито, ID таблицы и т.д.).
- `credentials.json` — ключ Google-сервисаккаунта.

Если ты сохранял копии `.env` и `credentials.json` — просто положишь их обратно (шаг 4). Если нет — в шаге 4 расписано, откуда каждое значение взять заново.

---

## 1. Заказать сервер на Бегете

1. Бегет → раздел **«Облачные серверы» (VPS)** — не обычный хостинг, а именно VPS с root-доступом (нужен Docker).
2. ОС: **Ubuntu 22.04**. Тариф: минимальный с запасом (2 CPU / 2–4 ГБ RAM хватает на все три бота).
3. Получи **IP нового сервера** и root-доступ (пароль или SSH-ключ). Запиши IP — он понадобится для DNS.

---

## 2. Базовая настройка сервера

Зайди по SSH (Termius) под root и выполни:

```bash
apt update && apt upgrade -y
curl -fsSL https://get.docker.com | sh            # Docker + compose-плагин
apt install -y nginx certbot python3-certbot-nginx git
systemctl enable --now docker nginx
```

---

## 3. Забрать код с GitHub

```bash
mkdir -p ~/app && cd ~/app
git clone https://rimskiymedved:ВСТАВЬ_ТОКЕН@github.com/rimskiymedved/muza_bot.git
cd muza_bot
```

`ВСТАВЬ_ТОКЕН` — GitHub Personal Access Token (github.com/settings/tokens, classic, scope `repo`). Тот, что был засвечен, лучше пересоздать.

---

## 4. Восстановить секреты (.env и credentials.json)

### 4.1 credentials.json (ключ Google)
- **Есть копия** → положи файл в `~/app/muza_bot/credentials.json`.
- **Нет копии** → Google Cloud Console → тот же проект → сервисный аккаунт → **Keys → Add key → JSON**, скачай, переименуй в `credentials.json`, положи в папку. Затем **расшарь Google-таблицу** на email этого сервисаккаунта (Доступ → добавить email из `client_email` в json, права «Редактор»).

### 4.2 .env
- **Есть копия** → положи её как `~/app/muza_bot/.env`.
- **Нет копии** → создай `nano .env` по шаблону `.env.example` и заполни:

| Переменная | Где взять |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather → /mybots → бот Музы → API Token |
| `SUPERADMIN_ID` | твой Telegram ID = `45028744` |
| `NOTIFY_USERNAME` | `@rimskiymedved` |
| `WEBAPP_URL` | `https://bot.muzal-moscow.ru` |
| `SPREADSHEET_ID` | из URL Google-таблицы: docs.google.com/spreadsheets/d/**ЭТО**/edit |
| `GOOGLE_CREDENTIALS_PATH` | `credentials.json` |
| `SHEET_NAME` / `FREE_SHEET_NAME` / `LEADS_SHEET_NAME` | `Бронирования` / `Свободные` / `Авито` |
| `AVITO_CLIENT_ID` / `AVITO_CLIENT_SECRET` | кабинет Авито → настройки API/интеграций (можно перевыпустить) |
| `AVITO_ACCOUNT_NAME` | `Муза` |
| `AVITO_NOTIFY_GROUP_ID` | ID Telegram-группы «МузаЛ / АВИТО» (можно узнать через @getidsbot, добавив его в группу) |
| `GROQ_API_KEY` | console.groq.com → API Keys (для голосовых команд) |
| `HEALTHCHECK_URL` | пусто (или ping-URL Healthchecks.io) |

Права на файлы (секреты не должны быть в git — они и так в .gitignore):
```bash
chmod 600 .env credentials.json
```

---

## 5. Перенаправить домен на новый сервер

В панели **reg.ru** → управление зоной домена `muzal-moscow.ru`:
- измени **A-запись** `bot` → **новый IP сервера** (старый был 89.40.204.109).

Проверь распространение (ждать 5–30 мин):
```bash
dig +short bot.muzal-moscow.ru @8.8.8.8
```
Должен вернуть новый IP. Дальше — только когда вернёт правильный.

---

## 6. nginx + SSL

```bash
cat > /etc/nginx/sites-available/muza_bot <<'EOF'
server {
    listen 80;
    server_name bot.muzal-moscow.ru;
    client_max_body_size 10M;
    location / {
        proxy_pass         http://127.0.0.1:8001;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
EOF
ln -s /etc/nginx/sites-available/muza_bot /etc/nginx/sites-enabled/muza_bot
nginx -t && systemctl reload nginx
certbot --nginx -d bot.muzal-moscow.ru      # email — свой; редирект — вариант «2»
```

---

## 7. Запуск

```bash
cd ~/app/muza_bot
docker compose up -d --build
docker compose logs --tail=40 muza_bot
```

Признаки успеха в логах:
- `✅ схема готова`
- `✅ Синхронизировано бронирований: N` (N ≠ 0 — данные подтянулись из таблицы)
- `Бот запущен. Жду сообщения...`
- В Telegram придёт «🟢 Бот запущен».

---

## 8. Проверка

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://bot.muzal-moscow.ru/health
```
Ожидаем `200`. Затем открой мини-апп через кнопку бота в Telegram.

Адрес в @BotFather (Menu Button / Configure Mini App) остаётся прежним — `https://bot.muzal-moscow.ru`, менять не нужно, раз домен переехал на новый IP.

---

## 9. АртХаос и Онлайн-Авито (те же шаги)

Каждый — отдельный репозиторий + свой `.env` + свой домен:
- **АртХаос**: клон его репозитория в `~/app/ArtHaos_bot`, свой `.env`, домен `arthaoss.ru` → перенаправить A-запись на новый IP, добавить server-блок nginx (проксирует на его порт, был `127.0.0.1:8000`) + `certbot -d arthaoss.ru`.
- **Онлайн-Авито**: аналогично, свой репозиторий и настройки.
Порядок тот же: Docker → git clone → .env + credentials → домен → nginx+certbot → `docker compose up -d --build`.

---

## 10. Чтобы такого больше не было (сразу после восстановления)

- **Сохрани копии** `.env` и `credentials.json` в менеджер паролей — это единственное, чего нет в git.
- Пересоздай засвеченный GitHub-токен, старый отзови.
- (Опц.) Подключи **UptimeRobot** на `https://bot.muzal-moscow.ru/health` — узнаешь о падении сервера сразу, а не от менеджеров.
- (Опц.) Раз в день — бэкап `muza.db` (хотя источник правды — Google-таблицы).
