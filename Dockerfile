FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    gcc g++ make pkg-config \
    libcairo2-dev libpango1.0-dev libglib2.0-dev \
    gobject-introspection libgirepository1.0-dev \
    meson ninja-build \
    fonts-dejavu fonts-noto-color-emoji \
    fonts-noto-cjk fonts-noto-core fonts-khmeros \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 9122
CMD ["python", "app.py"]
