# The Telegram MCP server's image, for any container host or your own server (docker compose up). Settings and
# secrets come from the environment (README, Deploy); deploy.py's Lambda zip runs the same server.py.
# From ECR Public, outside Docker Hub's pull limit.
FROM public.ecr.aws/docker/library/python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 PORT=8000 TELEGRAM_SESSION_FILE=/data/session.txt

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY server.py login.py ./

# A uid no host account has. The server writes nothing; login.py writes ./data, so on Linux: chown 10790 data.
USER 10790:10790
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]
CMD ["python", "server.py"]
