FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip wheel \
 && pip install --no-cache-dir -r requirements.txt

COPY server.py ./
COPY app ./app

VOLUME ["/data"]
EXPOSE 8000

CMD ["python", "server.py"]
