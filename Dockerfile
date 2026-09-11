FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IMAGE_MATCHER_HOST=0.0.0.0 \
    IMAGE_MATCHER_PORT=5000

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY app.py image_matcher.py synology_cloud.py visual_search.py ./
COPY static ./static

EXPOSE 5000

CMD ["python", "app.py"]
