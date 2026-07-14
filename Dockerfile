FROM python:3.12-slim

WORKDIR /app
COPY server.py index.html ./
RUN mkdir -p /var/data

# No pip install is needed: SAM Radar uses only the Python standard library.
ENV PYTHONUNBUFFERED=1 \
    RADAR_DB_PATH=/var/data/radar.db \
    RADAR_SETTINGS_PATH=/var/data/settings.json

EXPOSE 8765
CMD ["python", "server.py"]
