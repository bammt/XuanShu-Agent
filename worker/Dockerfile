FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY alembic ./alembic
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev \
    && useradd --uid 10001 --create-home xuanshu \
    && mkdir -p /var/lib/xuanshu/workspaces \
    && chown -R xuanshu:xuanshu /var/lib/xuanshu
COPY worker/worker.py ./
COPY worker/studio_worker.py ./
USER xuanshu
CMD [".venv/bin/python","worker.py"]
