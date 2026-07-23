FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py db.py legal_engine.py entrypoint.py ./
COPY fetch/ ./fetch/

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["python", "entrypoint.py"]
