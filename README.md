# RSSHub Admin & Quality Control System

A comprehensive admin interface for monitoring, managing, and ensuring the quality of your RSSHub feeds.

## Features

- **Feed Dashboard**: Monitor all feeds at a glance with quality metrics
- **Feed Quality Scoring**: Automatic quality analysis of feed content
- **Content Preview**: View feed content directly in the admin panel
- **Alerting System**: Get notified of feed issues and degradation
- **Historical Analytics**: Track feed health over time
- **Easy Configuration**: Add and test RSSHub routes without code changes

## Installation

### Prerequisites

- Python 3.8 or higher
- A running RSSHub instance (can be set up with docker-compose)

### Option 1: Using Docker Compose (Recommended)

The included docker-compose.yml file sets up a complete environment with:
- RSSHub
- Browserless Chrome (for JavaScript rendering)
- Redis (for caching)
- RSSHub Admin

```bash
# Clone the repository
git clone https://github.com/yourusername/rsshub-admin.git
cd rsshub-admin

# Start the services
docker-compose up -d

# Access RSSHub Admin at http://localhost:5000
```

### Option 2: Manual Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/rsshub-admin.git
cd rsshub-admin

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Initialize database
flask --app app.py init-db

# Run the application
flask --app app.py run --host=0.0.0.0 --port=5000
```

## Configuration

### Environment Variables

- `FLASK_APP`: Set to `app.py`
- `FLASK_ENV`: `development` or `production`
- `RSSHUB_BASE_URL`: URL of your RSSHub instance (default: `http://localhost:1200`)
- `SECRET_KEY`: Secret key for Flask session
- `DATABASE_URL`: SQLAlchemy database URL (default: SQLite in instance folder)

### Application Settings

Additional settings can be configured through the Settings page in the admin interface:

- **RSSHub Base URL**: URL of your RSSHub instance
- **Check Interval**: Default interval for checking feeds (minutes)

## Usage

### Adding a Feed

1. Navigate to **Add Feed** page
2. Enter a name and RSSHub route (e.g., `nytimes/homepage`)
3. Click **Test Route** to verify the feed works
4. Set category and other optional settings
5. Save the feed

### Monitoring Feed Quality

The dashboard shows quality metrics for all feeds:

- **Quality Score**: 0-100 score based on content quality metrics
- **Success Rate**: Percentage of successful feed fetches
- **Content Length**: Average content length of feed items
- **Images**: Presence of images in feed content

### Handling Alerts

Alerts are generated for various feed issues:

- **Error**: Feed fetch failures or critical issues
- **Warning**: Low quality content or potential issues
- **Info**: System information and non-critical events

Click "Mark as Read" to acknowledge alerts.

## System Architecture

RSSHub Admin consists of:

1. **Flask Web App**: The admin interface
2. **SQLite Database**: Stores feed configurations and fetch logs
3. **Background Scheduler**: Periodically checks feeds
4. **Quality Analyzer**: Evaluates feed content quality

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgements

- [RSSHub](https://github.com/DIYgod/RSSHub) - The core RSS generation service
- [Flask](https://flask.palletsprojects.com/) - The web framework used
- [Bootstrap](https://getbootstrap.com/) - UI framework