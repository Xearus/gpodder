FROM docker.io/library/python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GPODDER_HOME=/data \
    GPODDER_INSTALL_UIS=cli

WORKDIR /app
COPY pyproject.toml setup.py setup.cfg README.md COPYING MANIFEST.in ./
COPY src ./src
COPY bin ./bin
COPY share ./share
RUN pip install --no-cache-dir ".[web]" \
    && useradd --uid 10001 --create-home gpodder \
    && mkdir /data \
    && chown gpodder:gpodder /data

USER gpodder
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD ["python", "-m", "gpodder.web.healthcheck"]
CMD ["uvicorn", "gpodder.web.app:app", "--host", "0.0.0.0", "--port", "8080"]

FROM runtime AS webtest
USER root
RUN pip install --no-cache-dir pytest pytest-httpserver pytest-cov httpx pycodestyle isort
COPY tests /tests
USER gpodder
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "/tests/test_web.py", "/tests/test_feedcore.py"]

FROM runtime AS production
