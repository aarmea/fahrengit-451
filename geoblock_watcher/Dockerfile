FROM python:3.13-alpine

RUN pip install --no-cache-dir pyyaml watchdog docker

WORKDIR /app
COPY watcher.py .

CMD ["python", "-u", "watcher.py"]
