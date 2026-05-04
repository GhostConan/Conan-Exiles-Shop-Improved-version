FROM python:3.11-slim

WORKDIR /app

# Install system deps needed by aiomysql / rcon
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create logs directory
RUN mkdir -p logs

CMD ["python", "-m", "bot.main"]
