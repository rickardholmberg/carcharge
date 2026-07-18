FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY carcharge/ carcharge/
COPY main.py .

# /data is a volume mount: put config.yaml (and token cache) there
VOLUME ["/data"]

CMD ["python", "main.py"]
