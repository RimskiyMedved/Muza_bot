# Используем легкую версию Python
FROM python:3.11-slim

# Указываем рабочую папку внутри контейнера
WORKDIR /app

# Шрифты для генерации карточек броней
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*

# Копируем список библиотек и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь остальной код бота
COPY . .

# Команда для запуска
CMD ["python", "bot.py"]
