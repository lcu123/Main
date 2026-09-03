FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md requirements.lock ./
COPY src ./src
# Pin every dependency before installing the package itself with no further
# resolution, so a rebuild months from now can't silently pick up a breaking
# release. Regenerate requirements.lock with `pip install -e . && pip freeze`
# in a clean venv when intentionally bumping versions.
RUN pip install --no-cache-dir -r requirements.lock && \
    pip install --no-cache-dir --no-deps .

ENV MCP_TRANSPORT=http \
    PORT=8080 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["fr-mcp"]
