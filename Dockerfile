# Dockerfile

# Stage 1: builder — install dependencies in isolation
FROM python:3.11-slim AS builder

WORKDIR /app

# Copy requirements first — Docker layer caching
# If requirements.txt hasn't changed, this layer is reused on rebuild
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# Stage 2: runtime — lean final image
FROM python:3.11-slim AS runtime

WORKDIR /app

# Copy installed packages from builder stage
COPY --from=builder /install /usr/local

# Copy application code
COPY app/ ./app/

# Don't run as root — security best practice
RUN adduser --disabled-password --no-create-home appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]