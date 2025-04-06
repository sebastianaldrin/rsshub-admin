from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class FeedSource(db.Model):
    """Model for RSS feed sources"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    category = db.Column(db.String(50), nullable=True)
    
    # RSSHub specific
    rsshub_route = db.Column(db.String(255), nullable=False, unique=True)
    original_url = db.Column(db.String(255), nullable=True)  # Original website URL
    
    # Config and status
    is_active = db.Column(db.Boolean, default=True)
    check_frequency = db.Column(db.Integer, default=30)  # Minutes
    requires_javascript = db.Column(db.Boolean, default=False)
    
    # Custom selectors (JSON stored as string)
    custom_selectors = db.Column(db.Text, nullable=True)
    
    # Metadata
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    fetch_logs = db.relationship('FetchLog', backref='feed_source', lazy=True, cascade="all, delete-orphan")
    feed_items = db.relationship('FeedItem', backref='feed_source', lazy=True, cascade="all, delete-orphan")
    
    def __repr__(self):
        return f'<FeedSource {self.name}>'


class FetchLog(db.Model):
    """Log of feed fetch attempts"""
    id = db.Column(db.Integer, primary_key=True)
    feed_source_id = db.Column(db.Integer, db.ForeignKey('feed_source.id'), nullable=False)
    
    # Status info
    status = db.Column(db.String(20), nullable=False)  # success, error, warning
    http_status = db.Column(db.Integer, nullable=True)
    item_count = db.Column(db.Integer, default=0)
    error_message = db.Column(db.Text, nullable=True)
    
    # Quality metrics
    avg_title_length = db.Column(db.Float, nullable=True)
    avg_content_length = db.Column(db.Float, nullable=True)
    images_count = db.Column(db.Integer, nullable=True)
    quality_score = db.Column(db.Float, nullable=True)  # 0-100
    
    # Timing
    fetch_duration = db.Column(db.Float, nullable=True)  # seconds
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<FetchLog {self.feed_source_id} {self.status}>'


class FeedItem(db.Model):
    """Individual items from a feed"""
    id = db.Column(db.Integer, primary_key=True)
    feed_source_id = db.Column(db.Integer, db.ForeignKey('feed_source.id'), nullable=False)
    
    # Content
    title = db.Column(db.String(255), nullable=False)
    link = db.Column(db.String(255), nullable=False)
    guid = db.Column(db.String(255), nullable=True)
    description = db.Column(db.Text, nullable=True)
    content = db.Column(db.Text, nullable=True)
    author = db.Column(db.String(100), nullable=True)
    
    # Media
    image_url = db.Column(db.String(255), nullable=True)
    
    # Dates
    published_at = db.Column(db.DateTime, nullable=True)
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Quality
    has_full_content = db.Column(db.Boolean, default=False)
    word_count = db.Column(db.Integer, nullable=True)
    quality_issues = db.Column(db.Text, nullable=True)  # JSON list of issues
    
    def __repr__(self):
        return f'<FeedItem {self.title[:30]}>'


class SystemSettings(db.Model):
    """System-wide settings"""
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=True)
    description = db.Column(db.String(255), nullable=True)
    
    def __repr__(self):
        return f'<SystemSettings {self.key}>'


class Alert(db.Model):
    """System alerts"""
    id = db.Column(db.Integer, primary_key=True)
    feed_source_id = db.Column(db.Integer, db.ForeignKey('feed_source.id'), nullable=True)
    
    level = db.Column(db.String(20), nullable=False)  # info, warning, error
    message = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<Alert {self.level} {self.message[:30]}>'