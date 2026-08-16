FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e .

# No ENTRYPOINT/CMD - each daemon service in docker-compose.yml supplies its
# own `command: python -m timberdoodle.<module>` against this one image.
