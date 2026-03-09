FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py aggregator.py .

EXPOSE 8080
EXPOSE 8090

CMD ["python", "app.py"]
