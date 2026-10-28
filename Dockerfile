# The Telegram MCP server for local runs (docker compose up). AWS runs the same server.py from deploy.py's zip.
# From ECR Public, outside Docker Hub's pull limit.
FROM public.ecr.aws/docker/library/python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 PORT=8000 TELEGRAM_SESSION_FILE=/data/session.txt

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY server.py login.py ./

# A uid no host account has; ./data must be writable by it for login.py (chown 10790 data on Linux).
USER 10790:10790
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]
CMD ["python", "server.py"]
