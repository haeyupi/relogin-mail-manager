FROM mcr.microsoft.com/playwright/python:v1.54.0-noble

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install chromium

COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8787

CMD ["python", "app.py", "serve", "--host", "0.0.0.0", "--port", "8787"]
