# Use an Ubuntu base so apt has tesseract packages
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# install python, pip, tesseract and dependencies
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    tesseract-ocr \
    libtesseract-dev \
    libjpeg-dev \
    libpng-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy app files
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python3 -m pip install --upgrade pip
RUN python3 -m pip install -r /app/requirements.txt

# Copy bot
COPY bot.py /app/bot.py

# Use a non-root user (safer)
RUN useradd -m botuser
USER botuser
ENV HOME=/home/botuser
WORKDIR /app

# Default command
CMD ["python3", "bot.py"]
