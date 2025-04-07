import os
import json
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_wtf import FlaskForm
from wtforms import StringField, BooleanField, TextAreaField, SelectField, IntegerField
from wtforms.validators import DataRequired, URL, Optional
from apscheduler.schedulers.background import BackgroundScheduler
import logging

from models import db, FeedSource, FetchLog, FeedItem, Alert, SystemSettings
from utils import (
    fetch_and_parse_feed, validate_rsshub_route, check_all_feeds,
    get_feed_health, get_feed_preview
)

# Create Flask app
app = Flask(__name__, instance_relative_config=True)

# Load default configuration, allowing env overrides
app.config.from_mapping(
    SECRET_KEY=os.getenv('SECRET_KEY', 'dev'),
    SQLALCHEMY_DATABASE_URI=os.getenv(
        'DATABASE_URL',
        'sqlite:///' + os.path.join(app.instance_path, 'app.db')
    ),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    RSSHUB_BASE_URL=os.getenv('RSSHUB_BASE_URL', 'http://localhost:1200'),
    CHECK_INTERVAL=int(os.getenv('CHECK_INTERVAL', 30)),
)

# Ensure the instance folder exists
try:
    os.makedirs(app.instance_path)
except OSError:
    pass

# Initialize extensions
db.init_app(app)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(app.instance_path, 'app.log')),
        logging.StreamHandler()
    ]
)

# Forms
class FeedSourceForm(FlaskForm):
    name = StringField('Name', validators=[DataRequired()])
    description = TextAreaField('Description')
    category = StringField('Category')
    rsshub_route = StringField('RSSHub Route', validators=[DataRequired()])
    original_url = StringField('Original Website URL', validators=[Optional(), URL()])
    is_active = BooleanField('Active', default=True)
    check_frequency = IntegerField('Check Frequency (minutes)', default=30)
    requires_javascript = BooleanField('Requires JavaScript', default=False)
    custom_selectors = TextAreaField('Custom Selectors (JSON)')

class SettingsForm(FlaskForm):
    rsshub_base_url = StringField('RSSHub Base URL', validators=[DataRequired(), URL()])
    check_interval = IntegerField('Default Check Interval (minutes)', default=30)

# Routes
@app.route('/')
def dashboard():
    # Get summary stats
    total_feeds = FeedSource.query.count()
    active_feeds = FeedSource.query.filter_by(is_active=True).count()
    
    # Get feeds with recent status
    feeds = db.session.query(
        FeedSource, 
        db.func.max(FetchLog.fetched_at).label('last_check'),
        FetchLog.status,
        FetchLog.quality_score
    ).outerjoin(
        FetchLog, FeedSource.id == FetchLog.feed_source_id
    ).group_by(
        FeedSource.id
    ).all()
    
    # Get recent alerts
    alerts = Alert.query.filter_by(is_read=False).order_by(Alert.created_at.desc()).limit(5).all()
    
    # Get feed counts by category
    categories = db.session.query(
        FeedSource.category, 
        db.func.count(FeedSource.id)
    ).group_by(
        FeedSource.category
    ).all()
    
    return render_template(
        'dashboard.html',
        feeds=feeds,
        total_feeds=total_feeds,
        active_feeds=active_feeds,
        alerts=alerts,
        categories=categories
    )

@app.route('/source-builder')
def source_builder():
    """Source Builder interface for easily creating new feeds"""
    return render_template('source_builder.html')

@app.route('/api/feed/add', methods=['POST'])
def api_add_feed():
    """API endpoint to add a new feed"""
    try:
        data = request.get_json()
        
        # Validate required fields
        if not data.get('name'):
            return jsonify({'error': 'Feed name is required'}), 400
            
        if not data.get('rsshub_route'):
            return jsonify({'error': 'RSSHub route is required'}), 400
        
        # Create new feed source
        feed = FeedSource(
            name=data.get('name'),
            description=data.get('description', ''),
            category=data.get('category', ''),
            rsshub_route=data.get('rsshub_route'),
            original_url=data.get('original_url', ''),
            is_active=data.get('is_active', True),
            check_frequency=data.get('check_frequency', 30),
            requires_javascript=data.get('requires_javascript', False),
            custom_selectors=data.get('custom_selectors', '')
        )
        
        db.session.add(feed)
        db.session.commit()
        
        # Fetch the feed for the first time
        fetch_and_parse_feed(feed)
        
        return jsonify({
            'success': True,
            'feed_id': feed.id,
            'message': 'Feed added successfully'
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error adding feed via API: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/feed/suggest-selectors', methods=['POST'])
def api_suggest_selectors():
    """API endpoint to suggest selectors for a given URL"""
    try:
        data = request.get_json()
        url = data.get('url')
        
        if not url:
            return jsonify({'error': 'URL is required'}), 400
        
        selectors = suggest_selectors(url)
        
        return jsonify({
            'success': True,
            'selectors': selectors
        })
    except Exception as e:
        app.logger.error(f"Error suggesting selectors: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/feed/extraction-stats/<int:feed_id>')
def api_feed_extraction_stats(feed_id):
    """Get content extraction statistics for a feed"""
    try:
        # Get all items for this feed
        items = FeedItem.query.filter_by(feed_source_id=feed_id).all()
        
        if not items:
            return jsonify({
                'error': 'No items found for this feed'
            }), 404
        
        # Initialize stats
        extraction_methods = {
            'content_field': 0,
            'content_encoded': 0,
            'description': 0,
            'summary': 0,
            'custom_selectors': 0,
            'none': 0
        }
        
        full_content_count = 0
        word_counts = []
        
        # Process each item
        for item in items:
            # Extract metadata if available
            metadata = {}
            if item.metadata:
                try:
                    metadata = json.loads(item.metadata)
                except json.JSONDecodeError:
                    pass
            
            # Count extraction methods
            method = metadata.get('extraction_method', 'none')
            if method in extraction_methods:
                extraction_methods[method] += 1
            else:
                extraction_methods['none'] += 1
            
            # Count full content items
            if item.has_full_content:
                full_content_count += 1
            
            # Collect word counts
            if item.word_count:
                word_counts.append(item.word_count)
        
        # Calculate averages
        avg_word_count = int(sum(word_counts) / len(word_counts)) if word_counts else 0
        
        # Clean up extraction methods (remove zeros)
        extraction_methods = {k: v for k, v in extraction_methods.items() if v > 0}
        
        return jsonify({
            'extraction_methods': extraction_methods,
            'full_content_count': full_content_count,
            'partial_content_count': len(items) - full_content_count,
            'avg_word_count': avg_word_count,
            'total_items': len(items)
        })
        
    except Exception as e:
        app.logger.error(f"Error getting extraction stats: {e}")
        return jsonify({
            'error': str(e)
        }), 500

@app.route('/feeds')
def feed_list():
    # Get query parameters
    category = request.args.get('category')
    status = request.args.get('status')
    search = request.args.get('search')
    
    # Base query
    query = db.session.query(
        FeedSource, 
        db.func.max(FetchLog.fetched_at).label('last_check'),
        FetchLog.status,
        FetchLog.quality_score
    ).outerjoin(
        FetchLog, FeedSource.id == FetchLog.feed_source_id
    ).group_by(
        FeedSource.id
    )
    
    # Apply filters
    if category:
        query = query.filter(FeedSource.category == category)
    
    if status:
        if status == 'active':
            query = query.filter(FeedSource.is_active == True)
        elif status == 'inactive':
            query = query.filter(FeedSource.is_active == False)
        elif status in ['success', 'error', 'warning']:
            query = query.filter(FetchLog.status == status)
    
    if search:
        query = query.filter(FeedSource.name.ilike(f'%{search}%'))
    
    # Execute query
    feeds = query.all()
    
    # Get categories for filter
    categories = db.session.query(
        FeedSource.category, 
        db.func.count(FeedSource.id)
    ).group_by(
        FeedSource.category
    ).all()
    
    return render_template(
        'feed_list.html',
        feeds=feeds,
        categories=categories,
        current_category=category,
        current_status=status,
        search=search
    )

@app.route('/feed/<int:feed_id>')
def feed_detail(feed_id):
    feed = FeedSource.query.get_or_404(feed_id)
    
    # Get recent logs
    logs = FetchLog.query.filter_by(
        feed_source_id=feed_id
    ).order_by(FetchLog.fetched_at.desc()).limit(10).all()
    
    # Get recent items
    items = FeedItem.query.filter_by(
        feed_source_id=feed_id
    ).order_by(FeedItem.published_at.desc()).limit(20).all()
    
    # Get feed health metrics
    health = get_feed_health(feed_id)
    
    return render_template(
        'feed_detail.html',
        feed=feed,
        logs=logs,
        items=items,
        health=health
    )

@app.route('/feed/add', methods=['GET', 'POST'])
def add_feed():
    form = FeedSourceForm()
    
    if form.validate_on_submit():
        # Validate RSSHub route
        is_valid, message = validate_rsshub_route(form.rsshub_route.data)
        
        if not is_valid:
            flash(f'Invalid RSSHub route: {message}', 'danger')
            return render_template('add_feed.html', form=form, mode='add')
        
        # Create new feed source
        feed = FeedSource(
            name=form.name.data,
            description=form.description.data,
            category=form.category.data,
            rsshub_route=form.rsshub_route.data,
            original_url=form.original_url.data,
            is_active=form.is_active.data,
            check_frequency=form.check_frequency.data,
            requires_javascript=form.requires_javascript.data,
            custom_selectors=form.custom_selectors.data
        )
        
        db.session.add(feed)
        
        try:
            db.session.commit()
            flash('Feed added successfully!', 'success')
            
            # Fetch the feed for the first time
            fetch_and_parse_feed(feed)
            
            return redirect(url_for('feed_detail', feed_id=feed.id))
        except Exception as e:
            db.session.rollback()
            flash(f'Error adding feed: {str(e)}', 'danger')
    
    return render_template('add_feed.html', form=form, mode='add')

@app.route('/feed/edit/<int:feed_id>', methods=['GET', 'POST'])
def edit_feed(feed_id):
    feed = FeedSource.query.get_or_404(feed_id)
    
    if request.method == 'GET':
        # Pre-populate the form
        form = FeedSourceForm(obj=feed)
    else:
        form = FeedSourceForm()
        
        if form.validate_on_submit():
            # Update feed source
            feed.name = form.name.data
            feed.description = form.description.data
            feed.category = form.category.data
            
            # Check if route changed
            if feed.rsshub_route != form.rsshub_route.data:
                # Validate new route
                is_valid, message = validate_rsshub_route(form.rsshub_route.data)
                
                if not is_valid:
                    flash(f'Invalid RSSHub route: {message}', 'danger')
                    return render_template('add_feed.html', form=form, mode='edit')
                
                feed.rsshub_route = form.rsshub_route.data
            
            feed.original_url = form.original_url.data
            feed.is_active = form.is_active.data
            feed.check_frequency = form.check_frequency.data
            feed.requires_javascript = form.requires_javascript.data
            feed.custom_selectors = form.custom_selectors.data
            
            try:
                db.session.commit()
                flash('Feed updated successfully!', 'success')
                
                # Re-fetch the feed if active
                if feed.is_active:
                    fetch_and_parse_feed(feed)
                
                return redirect(url_for('feed_detail', feed_id=feed.id))
            except Exception as e:
                db.session.rollback()
                flash(f'Error updating feed: {str(e)}', 'danger')
    
    return render_template('add_feed.html', form=form, mode='edit', feed=feed)

@app.route('/feed/delete/<int:feed_id>', methods=['POST'])
def delete_feed(feed_id):
    feed = FeedSource.query.get_or_404(feed_id)
    
    try:
        db.session.delete(feed)
        db.session.commit()
        flash('Feed deleted successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting feed: {str(e)}', 'danger')
    
    return redirect(url_for('feed_list'))

@app.route('/feed/check/<int:feed_id>', methods=['POST'])
def check_feed(feed_id):
    feed = FeedSource.query.get_or_404(feed_id)
    
    status, message, _, item_count = fetch_and_parse_feed(feed)
    
    flash(f'Feed check complete: {message}', 'info' if status == 'success' else 'warning')
    
    return redirect(url_for('feed_detail', feed_id=feed.id))

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    # Get current settings
    rsshub_base_url = app.config.get('RSSHUB_BASE_URL')
    check_interval = app.config.get('CHECK_INTERVAL')
    
    form = SettingsForm(
        rsshub_base_url=rsshub_base_url,
        check_interval=check_interval
    )
    
    if form.validate_on_submit():
        # Update settings
        for key, value in [
            ('RSSHUB_BASE_URL', form.rsshub_base_url.data),
            ('CHECK_INTERVAL', form.check_interval.data)
        ]:
            setting = SystemSettings.query.filter_by(key=key).first()
            
            if setting:
                setting.value = str(value)
            else:
                setting = SystemSettings(key=key, value=str(value))
                db.session.add(setting)
            
            # Also update app config
            app.config[key] = value
        
        try:
            db.session.commit()
            flash('Settings updated successfully!', 'success')
            
            # Restart the scheduler with new interval
            init_scheduler()
            
            return redirect(url_for('settings'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating settings: {str(e)}', 'danger')
    
    return render_template('settings.html', form=form)

@app.route('/alerts')
def alert_list():
    # Get all alerts
    alerts = Alert.query.order_by(Alert.created_at.desc()).all()
    
    return render_template('alerts.html', alerts=alerts)

@app.route('/alerts/read/<int:alert_id>', methods=['POST'])
def mark_alert_read(alert_id):
    alert = Alert.query.get_or_404(alert_id)
    
    alert.is_read = True
    db.session.commit()
    
    return redirect(url_for('alert_list'))

@app.route('/alerts/read-all', methods=['POST'])
def mark_all_alerts_read():
    Alert.query.update({Alert.is_read: True})
    db.session.commit()
    
    flash('All alerts marked as read', 'success')
    
    return redirect(url_for('alert_list'))

# API endpoints
# Add these API endpoints to app.py

@app.route('/api/stats', methods=['GET'])
def api_stats():
    """Get basic database stats"""
    total_feeds = FeedSource.query.count()
    total_items = FeedItem.query.count()
    total_logs = FetchLog.query.count()
    
    return jsonify({
        'total_feeds': total_feeds,
        'total_items': total_items,
        'total_logs': total_logs
    })

@app.route('/api/feed/check-all', methods=['POST'])
def api_check_all_feeds():
    """Trigger check of all active feeds"""
    try:
        count = check_all_feeds()
        return jsonify({
            'success': True,
            'count': count,
            'message': f'Started checking {count} feeds'
        })
    except Exception as e:
        app.logger.error(f"Error checking all feeds: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/scheduler/status', methods=['GET'])
def api_scheduler_status():
    """Get scheduler status"""
    try:
        scheduler_running = 'scheduler' in globals() and scheduler.running
        
        if scheduler_running:
            # Find the next run time for the check_all_feeds job
            jobs = scheduler.get_jobs()
            next_run = None
            
            for job in jobs:
                if job.func_ref.endswith('check_all_feeds'):
                    next_run = job.next_run_time.strftime('%Y-%m-%d %H:%M:%S')
                    break
        
        return jsonify({
            'running': scheduler_running,
            'next_run': next_run if scheduler_running else None
        })
    except Exception as e:
        app.logger.error(f"Error getting scheduler status: {e}")
        return jsonify({
            'running': False,
            'error': str(e)
        }), 500

@app.route('/api/scheduler/restart', methods=['POST'])
def api_restart_scheduler():
    """Restart the scheduler"""
    try:
        init_scheduler()
        return jsonify({
            'success': True,
            'message': 'Scheduler restarted successfully'
        })
    except Exception as e:
        app.logger.error(f"Error restarting scheduler: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/feed/toggle-status/<int:feed_id>', methods=['POST'])
def api_toggle_feed_status(feed_id):
    """Toggle feed active status"""
    feed = FeedSource.query.get_or_404(feed_id)
    
    try:
        data = request.get_json()
        new_status = data.get('status', not feed.is_active)
        
        feed.is_active = new_status
        db.session.commit()
        
        return jsonify({
            'success': True,
            'is_active': feed.is_active
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error toggling feed status: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/feed/preview', methods=['POST'])
def api_preview_feed():
    route = request.json.get('route')
    
    if not route:
        return jsonify({'error': 'No route provided'}), 400
    
    preview_items = get_feed_preview(route)
    
    if preview_items is None:
        return jsonify({'error': 'Failed to fetch preview'}), 400
    
    return jsonify({
        'success': True,
        'items': preview_items
    })

@app.route('/api/feed/validate', methods=['POST'])
def api_validate_feed():
    route = request.json.get('route')
    
    if not route:
        return jsonify({'error': 'No route provided'}), 400
    
    is_valid, message = validate_rsshub_route(route)
    
    return jsonify({
        'valid': is_valid,
        'message': message
    })

@app.route('/api/feed/stats/<int:feed_id>')
def api_feed_stats(feed_id):
    # Get logs for the last 30 days
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    
    logs = FetchLog.query.filter(
        FetchLog.feed_source_id == feed_id,
        FetchLog.fetched_at >= thirty_days_ago
    ).order_by(FetchLog.fetched_at).all()
    
    stats = {
        'dates': [],
        'quality_scores': [],
        'item_counts': [],
        'success_rate': [],
    }
    
    for log in logs:
        stats['dates'].append(log.fetched_at.strftime('%Y-%m-%d'))
        stats['quality_scores'].append(log.quality_score or 0)
        stats['item_counts'].append(log.item_count or 0)
        stats['success_rate'].append(1 if log.status == 'success' else 0)
    
    return jsonify(stats)

# Helper functions
def init_scheduler():
    """Initialize or restart the background scheduler"""
    global scheduler
    
    # Stop existing scheduler if it exists
    if 'scheduler' in globals() and scheduler.running:
        scheduler.shutdown()
    
    # Create new scheduler
    scheduler = BackgroundScheduler()
    
    # Add job to check all feeds
    interval = app.config.get('CHECK_INTERVAL', 30)
    scheduler.add_job(check_all_feeds, 'interval', minutes=interval)
    
    # Start scheduler
    scheduler.start()
    app.logger.info(f"Scheduler started with {interval} minute interval")

def load_settings():
    """Load settings from database into app config"""
    with app.app_context():
        settings = SystemSettings.query.all()
        
        for setting in settings:
            # Convert value to appropriate type
            if setting.key == 'CHECK_INTERVAL':
                app.config[setting.key] = int(setting.value)
            else:
                app.config[setting.key] = setting.value

# Create a command to initialize the database
@app.cli.command('init-db')
def init_db_command():
    """Create the database tables."""
    db.create_all()
    print('Initialized the database.')

# Create a command to load settings
@app.cli.command('load-settings')
def load_settings_command():
    """Load settings from database into app config."""
    settings = SystemSettings.query.all()
    
    for setting in settings:
        # Convert value to appropriate type
        if setting.key == 'CHECK_INTERVAL':
            app.config[setting.key] = int(setting.value)
        else:
            app.config[setting.key] = setting.value
    
    print('Settings loaded.')

# Create a command to start the scheduler
@app.cli.command('start-scheduler')
def start_scheduler_command():
    """Start the background scheduler."""
    init_scheduler()
    print('Scheduler started.')

# Setup code now happens with app context
def setup_app(app):
    with app.app_context():
        # Create tables
        db.create_all()
        
        # Load settings
        settings = SystemSettings.query.all()
        for setting in settings:
            if setting.key == 'CHECK_INTERVAL':
                app.config[setting.key] = int(setting.value)
            else:
                app.config[setting.key] = setting.value
        
        # Initialize scheduler
        init_scheduler()

# Modified app initialization
if __name__ == '__main__':
    # Get port from environment or use default
    port = int(os.environ.get('PORT', 5000))
    
    # Setup app
    setup_app(app)
    
    # Run the app
    app.run(host='0.0.0.0', port=port, debug=True)
else:
    # When imported as a module (e.g., by gunicorn)
    # Still perform setup when the module is imported
    app.before_request_funcs.setdefault(None, []).insert(0, lambda: setup_app(app) if not getattr(app, '_got_first_request', False) else None)