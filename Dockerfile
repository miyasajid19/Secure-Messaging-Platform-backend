FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=10000 \
    DATABASE_URL=sqlite:////data/app.db

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN apt-get update \
    && apt-get install --no-install-recommends -y gosu \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

COPY app ./app

# The entry command fixes mounted-disk ownership, then drops privileges.
RUN mkdir -p /data \
    && useradd --create-home --uid 10001 appuser \
    && chown appuser:appuser /data

EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os; from urllib.request import urlopen; urlopen('http://127.0.0.1:' + os.getenv('PORT', '10000') + '/health', timeout=3)" || exit 1

CMD ["sh", "-c", "chown appuser:appuser /data && exec gosu appuser sh -c 'python -m app.seed && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1'"]
