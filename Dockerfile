# JobSpy Lite — build the SPA, then install Python deps and run the API.
# Multi-stage: node builds frontend/dist, python-slim serves everything.

FROM node:20-slim AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app

# git is required: requirements.txt installs jobsniffer from a git URL.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY config.yaml ./
COPY run.py ./
COPY --from=frontend-build /build/dist ./frontend/dist

# DB / Excel / resume live on the mounted volume. config.yaml data_dir is "data".
RUN mkdir -p /app/data
ENV PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["python", "run.py", "--serve", "--host", "0.0.0.0", "--port", "8000"]
