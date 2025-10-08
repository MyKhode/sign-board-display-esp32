FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    gcc g++ make pkg-config \
    libcairo2-dev libpango1.0-dev libglib2.0-dev \
    gobject-introspection libgirepository1.0-dev \
    meson ninja-build fontconfig \
    fonts-dejavu fonts-noto-color-emoji \
    fonts-noto-cjk fonts-noto-core fonts-khmeros \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

# ✅ Install your custom Khmer fonts into Fontconfig so Pango can use them
RUN mkdir -p /usr/local/share/fonts/khmer && \
    cp -f /app/Bayon/*.ttf  /usr/local/share/fonts/khmer/  2>/dev/null || true && \
    cp -f /app/Bokor/*.ttf  /usr/local/share/fonts/khmer/  2>/dev/null || true && \
    cp -f /app/Koulen/*.ttf /usr/local/share/fonts/khmer/  2>/dev/null || true && \
    cp -f /app/Moul/*.ttf   /usr/local/share/fonts/khmer/  2>/dev/null || true && \
    fc-cache -f -v

EXPOSE 9122
CMD ["python", "app.py"]
