FROM python:3-slim

ENV TZ=America/Sao_Paulo

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc pkg-config curl tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip setuptools wheel --no-cache-dir

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]