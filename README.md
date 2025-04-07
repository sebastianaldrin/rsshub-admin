# RSSHub Admin & Quality Control System

A comprehensive admin interface for monitoring, managing, and ensuring the quality of your RSSHub feeds.

## Features

- **Feed Dashboard**: Monitor all feeds at a glance with quality metrics
- **Feed Quality Scoring**: Automatic quality analysis of feed content
- **Content Preview**: View feed content directly in the admin panel
- **Source Builder**: Easily add new sources without knowing routes
- **Smart Fallbacks**: Automatic content extraction with multiple fallback methods
- **Extraction Analytics**: Track which methods work best for each source
- **Alerting System**: Get notified of feed issues and degradation
- **Historical Analytics**: Track feed health over time

## Installation

### Prerequisites

- Docker and Docker Compose
- Git

### Using Docker Compose (Recommended)

The included docker-compose.yml file sets up a complete environment with:
- RSSHub
- Browserless Chrome (for JavaScript rendering)
- Redis (for caching)
- RSSHub Admin

```bash
# Clone the repository
git clone https://github.com/sebastianaldrin/rsshub-admin.git
cd rsshub-admin

# Start the services
docker-compose up -d

# Access RSSHub Admin at http://localhost:5000
```

## Configuration

### Environment Variables

Configure these in the `docker-compose.yml` file:

- `FLASK_APP`: Set to `app.py`
- `FLASK_ENV`: `development` or `production`
- `RSSHUB_BASE_URL`: URL of your RSSHub instance (default: `http://rsshub:1200`)
- `SECRET_KEY`: Secret key for Flask session
- `DATABASE_URL`: SQLAlchemy database URL (default: SQLite in instance folder)

### Application Settings

Additional settings can be configured through the Settings page in the admin interface:

- **RSSHub Base URL**: URL of your RSSHub instance
- **Check Interval**: Default interval for checking feeds (minutes)

## Usage

### Adding a Feed

#### Quick Method (Source Builder)
1. Go to the **Source Builder** page
2. Select a site type (Reddit, Twitter, etc.)
3. Fill in the required information
4. Click **Test Source** to verify
5. Save your new source

#### Advanced Method
1. Navigate to **Add Feed** page
2. Enter a name and RSSHub route (e.g., `nytimes/homepage`)
3. Click **Test Route** to verify the feed works
4. Set category and other optional settings
5. Save the feed

### Custom Selectors

For sites needing custom extraction:
1. Go to the **Source Builder**
2. Select the **Custom Selectors** tab
3. Enter the website URL
4. Click **Suggest Selectors** for automatic analysis
5. Adjust selectors as needed
6. Test and save your source

### Monitoring Feed Quality

The dashboard shows quality metrics for all feeds:

- **Quality Score**: 0-100 score based on content quality metrics
- **Success Rate**: Percentage of successful feed fetches
- **Content Length**: Average content length of feed items
- **Images**: Presence of images in feed content

### Extraction Analytics

The feed detail page now shows extraction method analytics:
- View which methods worked best (content field, description, custom selectors)
- See fallback statistics
- Track full content vs. partial content success rates

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
5. **Universal Scraper**: Handles content extraction with fallbacks
6. **Source Builder**: GUI for easily adding new sources

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgements

- [RSSHub](https://github.com/DIYgod/RSSHub) - The core RSS generation service
- [Flask](https://flask.palletsprojects.com/) - The web framework used
- [Bootstrap](https://getbootstrap.com/) - UI framework