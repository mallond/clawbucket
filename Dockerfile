FROM sipeed/picoclaw:latest AS picoclaw_src

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=picoclaw_src /usr/local/bin/picoclaw /usr/local/bin/picoclaw
COPY picoclaw.config.json /root/.picoclaw/config.json

COPY app.py aggregator.py game_engine.py .

EXPOSE 8080
EXPOSE 8090

CMD ["python", "app.py"]
