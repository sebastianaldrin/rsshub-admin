FROM python:3.9-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create directory structure
RUN mkdir -p static/css static/js instance

# Download static dependencies
RUN apt-get update && apt-get install -y curl \
    && curl -L https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css -o static/css/bootstrap.min.css \
    && curl -L https://cdn.jsdelivr.net/npm/chart.js@3.7.0/dist/chart.min.js -o static/js/chart.min.js \
    && touch static/css/main.css \
    && touch static/js/main.js \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Copy application code
COPY . .

# Set environment variables
ENV FLASK_APP=app.py
ENV FLASK_ENV=production

# Expose port
EXPOSE 5000

# Run the application with gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]