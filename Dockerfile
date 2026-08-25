FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e ".[validate]"

# No ENTRYPOINT/CMD - each daemon service in docker-compose.yml supplies its
# own `command: python -m timberdoodle.<module>` against this one image.
# `validate` (pyshacl) is included here because validate_api.py runs as one
# of the daemons sharing this same image - the extras split in pyproject.toml
# is about the bare-metal/local dev install, not this containerized one.
