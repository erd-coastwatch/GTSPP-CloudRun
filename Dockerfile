# Based on erd-coastwatch/cloud-run-template.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY config/requirements.txt ./config/
RUN pip install --no-cache-dir -r config/requirements.txt
COPY . .
ENV PYTHONPATH="/app:/app/scripts" PYTHONUNBUFFERED=1
RUN chmod +x /app/entrypoint.sh
ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
