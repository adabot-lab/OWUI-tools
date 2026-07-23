FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py db.py legal_engine.py ./
COPY extract/ ./extract/

# Input directory for law markdown files
# data/ directory is created at runtime

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["python", "main.py"]
