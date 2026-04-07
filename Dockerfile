FROM python:3.11-slim

WORKDIR /app

# tzdata + tzdata-legacy: ib_insync parses IBKR timestamps with deprecated
# aliases like "US/Central" which only exist in the legacy zone DB.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata tzdata-legacy \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config/ config/
COPY src/ src/

RUN mkdir -p logs data span_data

CMD ["python3", "-m", "src.main"]
