FROM python:3.9-slim

WORKDIR /app

# Install system dependencies for newspaper3k
RUN apt-get update && apt-get install -y \
    python3-dev \
    libxml2-dev \
    libxslt-dev \
    libjpeg-dev \
    zlib1g-dev \
    libpng-dev \
    curl \
    && curl -L https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css -o static/css/bootstrap.min.css \
    && curl -L https://cdn.jsdelivr.net/npm/chart.js@3.7.0/dist/chart.min.js -o static/js/chart.min.js \
    && mkdir -p static/css static/js instance \
    && touch static/css/main.css \
    && touch static/js/main.js \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download NLTK data for newspaper3k
RUN python -m nltk.downloader punkt

# Copy application code
COPY . .

# Set environment variables
ENV FLASK_APP=app.py
ENV FLASK_ENV=production

# Expose port
EXPOSE 5000

# Run the application with gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]