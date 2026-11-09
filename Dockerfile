FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 eventdesk \
    && mkdir -p /app/data \
    && chown eventdesk:eventdesk /app/data
COPY app.py setup.py ./
COPY static ./static
USER eventdesk
EXPOSE 8080
CMD ["python", "app.py"]
