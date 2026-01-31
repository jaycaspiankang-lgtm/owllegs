FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for EasyOCR
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

ENV CUDA_VISIBLE_DEVICES ""

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the bot files
COPY bot.py telegram_bot.py ./

# Create data directory for persistent storage
RUN mkdir -p /data

ENTRYPOINT ["/bin/bash"]