FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY connector.py data_model.py api.py ./
COPY frontend/ ./frontend/

RUN mkdir -p data/model

EXPOSE 8000

# Use shell form so $PORT is expanded at runtime
CMD python connector.py && python data_model.py && uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}
