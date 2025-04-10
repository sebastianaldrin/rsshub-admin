import feedparser
import requests
from bs4 import BeautifulSoup
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin
from flask import current_app
from models import db, FeedSource, FetchLog, FeedItem, Alert
import newspaper
from newspaper import Article, build
import hashlib
import logging
import traceback

# Configure logging
logger = logging.getLogger(__name__)

def normalize_datetime(dt):
    """Make all datetimes timezone-naive for consistent comparison"""
    if dt is None:
        return datetime.utcnow()
    if dt.tzinfo is not None:
        # Convert to UTC and remove timezone info
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt

def fetch_and_parse_feed(feed_source, save_items=True):
    """
    Fetch and parse an RSS feed from a FeedSource with enhanced fallback handling
    
    Args:
        feed_source: FeedSource object
        save_items: Whether to save parsed items to database
    
    Returns:
        tuple: (status, message, feed_data, items_count)
    """
    start_time = time.time()
    
    # Check if this is a custom route
    if feed_source.rsshub_route.startswith('custom/'):
        # This is a custom route, use newspaper3k for smart extraction
        url = feed_source.original_url
        if not url:
            return 'error', 'Original URL is required for custom routes', None, 0
        
        try:
            # Clean up the URL if needed
            if not url.startswith('http'):
                url = 'https://' + url
            
            logger.info(f"Processing custom route for: {url}")
            
            # Build a newspaper source with advanced configuration
            news_source = build(url, memoize_articles=False)
            
            # Set source to fetch article details (otherwise only gets partial info)
            news_source.config.fetch_images = False  # Skip image downloading for speed
            news_source.config.request_timeout = 20  # Timeout in seconds
            news_source.config.number_threads = 4   # Use 4 threads for parallel downloading
            
            # Download and parse the source
            news_source.download()
            news_source.parse()
            
            # Download the homepage to try to detect article links
            response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
            homepage_html = response.text
            
            # Get articles from newspaper extraction
            feed_items = []
            
            # First try to get articles from newspaper's built-in article extraction
            articles = news_source.articles[:15]  # Limit to 15 articles for performance
            
            # If newspaper didn't extract enough articles, try manually finding links
            if len(articles) < 3:
                logger.info(f"Few articles detected by newspaper, trying manual link extraction")
                
                # Parse the homepage with BeautifulSoup
                soup = BeautifulSoup(homepage_html, 'html.parser')
                
                # Find potential article links
                article_links = find_article_links(soup, url)
                
                # Create Article objects for these links
                for link in article_links[:15]:  # Limit to 15
                    try:
                        article = Article(url=link, language='en')  # Default to English
                        articles.append(article)
                    except Exception as e:
                        logger.error(f"Error creating Article for {link}: {str(e)}")
            
            logger.info(f"Found {len(articles)} articles to process")
            
            # Multi-threaded download for performance
            newspaper.news_pool.set(articles, threads_per_source=4)
            newspaper.news_pool.join()
            
            # Process each article
            for article in articles:
                try:
                    # Parse article if not already parsed
                    if not article.is_parsed:
                        article.parse()
                    
                    # Get article text
                    if not article.text or len(article.text.strip()) < 150:
                        logger.info(f"Skipping article with insufficient text: {article.url}")
                        continue
                    
                    # Get or generate article HTML
                    html_content = article.article_html
                    if not html_content and article.text:
                        # Convert plain text to HTML paragraphs if article_html not available
                        paragraphs = article.text.split('\n\n')
                        html_content = ''.join([f"<p>{p}</p>" for p in paragraphs if p.strip()])
                    
                    # Create a unique identifier for this article
                    article_guid = hashlib.md5(article.url.encode()).hexdigest()
                    
                    # Create feed item
                    item = {
                        'title': article.title,
                        'link': article.url,
                        'guid': article_guid,
                        'description': article.text[:280] + '...' if len(article.text) > 280 else article.text,
                        'content': html_content,
                        'image_url': article.top_image,
                        'published_at': normalize_datetime(article.publish_date),
                        'author': ', '.join(article.authors) if article.authors else None,
                        'has_full_content': True,
                        'word_count': len(article.text.split()) if article.text else 0,
                    }
                    
                    # Add extraction metadata
                    item['extraction_metadata'] = json.dumps({
                        "extraction_method": "newspaper3k",
                        "content_length": len(html_content) if html_content else 0,
                        "keywords": article.keywords if hasattr(article, 'keywords') else []
                    })
                    
                    feed_items.append(item)
                except Exception as e:
                    error_details = traceback.format_exc()
                    logger.error(f"Error processing article {article.url}: {str(e)}\n{error_details}")
                    continue
            
            # Filter out items without title or content
            feed_items = [item for item in feed_items if item['title'] and item['content']]
            
            # Sort by publication date (newest first)
            feed_items.sort(key=lambda x: normalize_datetime(x['published_at']), reverse=True)
            
            # If no items could be extracted, return an error
            if not feed_items:
                return 'error', 'No articles could be extracted from the website', None, 0
            
            if save_items:
                # Clear existing items for this source
                FeedItem.query.filter_by(feed_source_id=feed_source.id).delete()
                
                # Add new items to the database
                for item in feed_items:
                    feed_item = FeedItem(
                        feed_source_id=feed_source.id,
                        title=item['title'],
                        link=item['link'],
                        guid=item['guid'],
                        description=item.get('description', ''),
                        content=item['content'],
                        author=item.get('author'),
                        image_url=item.get('image_url'),
                        published_at=normalize_datetime(item.get('published_at')),
                        has_full_content=item.get('has_full_content', False),
                        word_count=item.get('word_count', 0),
                        extraction_metadata=item.get('extraction_metadata')
                    )
                    db.session.add(feed_item)
                
                db.session.commit()
            
            # Calculate quality metrics
            avg_content_length = sum(len(item['content']) for item in feed_items) / len(feed_items) if feed_items else 0
            image_count = sum(1 for item in feed_items if item.get('image_url')) or 0
            
            # Calculate quality score
            quality_score = calculate_quality_score(
                item_count=len(feed_items),
                avg_content_length=avg_content_length,
                image_ratio=image_count / len(feed_items) if feed_items else 0
            )
            
            # Create fetch log
            fetch_duration = time.time() - start_time
            fetch_log = FetchLog(
                feed_source_id=feed_source.id,
                status='success',
                item_count=len(feed_items),
                avg_content_length=avg_content_length,
                images_count=image_count,
                quality_score=quality_score,
                fetch_duration=fetch_duration
            )
            db.session.add(fetch_log)
            db.session.commit()
            
            return 'success', f'Successfully extracted {len(feed_items)} articles from the website', None, len(feed_items)
            
        except Exception as e:
            error_details = traceback.format_exc()
            error_msg = f"{str(e)}: {error_details}"
            logger.error(f"Error processing custom route: {error_msg}")
            
            # Create error log
            fetch_log = FetchLog(
                feed_source_id=feed_source.id,
                status='error',
                error_message=str(e),
                fetch_duration=time.time() - start_time
            )
            db.session.add(fetch_log)
            
            # Create alert
            create_alert(
                feed_source.id, 
                'error', 
                f"Failed to process custom route: {feed_source.name} - {str(e)}"
            )
            
            db.session.commit()
            return 'error', str(e), None, 0
    
    # Standard RSSHub route processing (unchanged)
    rsshub_base_url = current_app.config.get('RSSHUB_BASE_URL')
    if not rsshub_base_url:
        return 'error', 'RSSHub base URL not configured', None, 0
    
    # Remove leading slash if present
    route = feed_source.rsshub_route.lstrip('/')
    full_url = urljoin(rsshub_base_url, route)
    
    try:
        # Fetch the feed - with timeout and retry logic
        max_retries = 2
        retry_count = 0
        response = None
        
        while retry_count <= max_retries:
            try:
                response = requests.get(full_url, timeout=30)
                response.raise_for_status()
                break
            except requests.exceptions.RequestException as e:
                retry_count += 1
                if retry_count > max_retries:
                    raise
                current_app.logger.warning(f"Retry {retry_count} for {feed_source.name}: {str(e)}")
                time.sleep(1)  # Short delay before retry
        
        # Parse the feed
        feed_data = feedparser.parse(response.content)
        
        # Check if feed is valid
        if not feed_data.entries:
            message = "Feed parsed but contains no items"
            status = "warning"
        else:
            message = f"Successfully fetched {len(feed_data.entries)} items"
            status = "success"
        
        # Calculate quality metrics
        title_lengths = []
        content_lengths = []
        image_count = 0
        
        # Custom selectors handling
        custom_selectors = None
        if feed_source.custom_selectors:
            try:
                custom_selectors = json.loads(feed_source.custom_selectors)
            except json.JSONDecodeError:
                current_app.logger.warning(f"Invalid custom_selectors JSON for {feed_source.name}")
        
        # Process feed items
        if save_items and feed_data.entries:
            # Clear existing items for this source
            FeedItem.query.filter_by(feed_source_id=feed_source.id).delete()
            
            # Add new items
            for entry in feed_data.entries:
                # Extract data with fallbacks
                title = getattr(entry, 'title', 'No Title')
                link = getattr(entry, 'link', '')
                guid = getattr(entry, 'id', link)
                description = getattr(entry, 'summary', '')
                
                # Multi-tiered content extraction with fallbacks
                content = ''
                extraction_method = 'none'
                
                # Method 1: Try content field (best case)
                if hasattr(entry, 'content'):
                    content = entry.content[0].value
                    extraction_method = 'content_field'
                
                # Method 2: Try content_encoded field
                elif hasattr(entry, 'content_encoded'):
                    content = entry.content_encoded
                    extraction_method = 'content_encoded'
                
                # Method 3: Try description field
                elif hasattr(entry, 'description'):
                    content = entry.description
                    extraction_method = 'description'
                
                # Method 4: Use summary as fallback
                elif description:
                    content = description
                    extraction_method = 'summary'
                
                # Method 5: Apply custom selectors if available
                if custom_selectors and link and (not content or len(content) < 200):
                    try:
                        content_from_selectors = fetch_content_with_selectors(link, custom_selectors)
                        if content_from_selectors:
                            content = content_from_selectors
                            extraction_method = 'custom_selectors'
                    except Exception as e:
                        current_app.logger.error(f"Error with custom selectors for {link}: {str(e)}")
                
                # Get author with fallbacks
                author = None
                if hasattr(entry, 'author'):
                    author = entry.author
                elif hasattr(entry, 'author_detail') and hasattr(entry.author_detail, 'name'):
                    author = entry.author_detail.name
                
                # Get publication date
                published_at = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    try:
                        published_at = datetime(*entry.published_parsed[:6])
                    except:
                        # Try alternative date fields
                        if hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                            try:
                                published_at = datetime(*entry.updated_parsed[:6])
                            except:
                                pass
                
                # Find image with multiple fallback methods
                image_url = None
                
                # Method 1: Check media_content
                if hasattr(entry, 'media_content') and entry.media_content:
                    for media in entry.media_content:
                        if 'url' in media and (
                            'image' in media.get('type', '') or 
                            media.get('url', '').lower().endswith(('.jpg', '.jpeg', '.png', '.gif'))
                        ):
                            image_url = media.get('url')
                            break
                
                # Method 2: Check enclosures
                if not image_url and hasattr(entry, 'enclosures') and entry.enclosures:
                    for enclosure in entry.enclosures:
                        if 'type' in enclosure and 'image' in enclosure.get('type', ''):
                            image_url = enclosure.get('href', None) or enclosure.get('url', None)
                            if image_url:
                                break
                
                # Method 3: Extract from content HTML
                if not image_url and content:
                    soup = BeautifulSoup(content, 'lxml')
                    img_tag = soup.find('img')
                    if img_tag and img_tag.get('src'):
                        image_url = img_tag.get('src')
                
                # Method 4: Try to get from media_thumbnail
                if not image_url and hasattr(entry, 'media_thumbnail') and entry.media_thumbnail:
                    for thumbnail in entry.media_thumbnail:
                        if 'url' in thumbnail:
                            image_url = thumbnail['url']
                            break
                
                # Count images for metrics
                if image_url:
                    image_count += 1
                
                # Calculate quality metrics
                title_lengths.append(len(title))
                
                # Count real text content (exclude HTML)
                text_content = ''
                if content:
                    text_content = BeautifulSoup(content, 'lxml').get_text()
                content_lengths.append(len(text_content))
                
                # Check for full content
                has_full_content = len(text_content) > 200
                
                # Count words
                word_count = len(re.findall(r'\w+', text_content)) if text_content else 0
                
                # Check for quality issues
                quality_issues = []
                if not title or len(title) < 10:
                    quality_issues.append("short_title")
                if not content or len(text_content) < 100:
                    quality_issues.append("short_content")
                if not image_url:
                    quality_issues.append("no_image")
                
                # Create new feed item with extraction method tracking
                item = FeedItem(
                    feed_source_id=feed_source.id,
                    title=title,
                    link=link,
                    guid=guid,
                    description=description,
                    content=content,
                    author=author,
                    image_url=image_url,
                    published_at=normalize_datetime(published_at),
                    has_full_content=has_full_content,
                    word_count=word_count,
                    quality_issues=json.dumps(quality_issues) if quality_issues else None,
                    extraction_metadata=json.dumps({
                        "extraction_method": extraction_method,
                        "content_length": len(text_content) if text_content else 0
                    })
                )
                db.session.add(item)
            
            # Commit all items at once
            db.session.commit()
        
        # Calculate average metrics
        avg_title_length = sum(title_lengths) / len(title_lengths) if title_lengths else 0
        avg_content_length = sum(content_lengths) / len(content_lengths) if content_lengths else 0
        
        # Calculate quality score (0-100)
        quality_score = calculate_quality_score(
            item_count=len(feed_data.entries),
            avg_content_length=avg_content_length,
            image_ratio=image_count / len(feed_data.entries) if feed_data.entries else 0
        )
        
        # Create fetch log
        fetch_duration = time.time() - start_time
        fetch_log = FetchLog(
            feed_source_id=feed_source.id,
            status=status,
            http_status=response.status_code,
            item_count=len(feed_data.entries),
            avg_title_length=avg_title_length,
            avg_content_length=avg_content_length,
            images_count=image_count,
            quality_score=quality_score,
            fetch_duration=fetch_duration
        )
        db.session.add(fetch_log)
        db.session.commit()
        
        # Create alert if quality is low
        if quality_score < 50 and status == 'success':
            create_alert(
                feed_source.id,
                'warning',
                f"Low quality feed: {feed_source.name} (Score: {quality_score:.1f}/100)"
            )
        
        return status, message, feed_data, len(feed_data.entries)
        
    except requests.exceptions.RequestException as e:
        error_msg = str(e)
        
        # Create error log
        fetch_log = FetchLog(
            feed_source_id=feed_source.id,
            status='error',
            http_status=getattr(e.response, 'status_code', None) if hasattr(e, 'response') else None,
            error_message=error_msg,
            fetch_duration=time.time() - start_time
        )
        db.session.add(fetch_log)
        
        # Create alert
        create_alert(
            feed_source.id, 
            'error', 
            f"Failed to fetch feed: {feed_source.name} - {error_msg}"
        )
        
        db.session.commit()
        return 'error', error_msg, None, 0


def find_article_links(soup, base_url):
    """
    Find article links on a homepage or listing page.
    
    Args:
        soup: BeautifulSoup object
        base_url: Base URL for resolving relative links
        
    Returns:
        list: List of article URLs
    """
    article_urls = []
    
    # Find all links
    links = soup.find_all('a', href=True)
    
    # Process each link
    for link in links:
        href = link['href']
        
        # Skip non-article links
        if not href or href.startswith('#') or href.startswith('javascript:'):
            continue
        
        # Skip social media, login, search, category links
        skip_patterns = [
            '/search', '/login', '/signup', '/register', '/account',
            '/tag/', '/category/', '/author/', '/about/', '/contact',
            'facebook.com', 'twitter.com', 'instagram.com', 'youtube.com',
            'pinterest.com', 'linkedin.com', 'rss', 'feed', 'subscribe'
        ]
        if any(pattern in href.lower() for pattern in skip_patterns):
            continue
        
        # Resolve relative URLs
        full_url = urljoin(base_url, href)
        
        # Skip URLs that aren't from the same domain
        if not full_url.startswith(base_url) and not full_url.startswith(urlparse(base_url).scheme + '://' + urlparse(base_url).netloc):
            continue
        
        # Skip URLs that look like category pages, tag pages, etc.
        path = urlparse(full_url).path
        if path == '/' or path == '':
            continue
        
        # Exclude pagination links
        if re.search(r'/page/\d+', path) or re.search(r'\?page=\d+', full_url):
            continue
        
        # Look for article URL patterns
        article_indicators = [
            # URL patterns that suggest an article
            '/article/', '/story/', '/news/', '/post/', 
            '.html', '.htm', '.php', '.asp',
            # URL patterns with dates
            r'\d{4}/\d{2}/\d{2}',
            # URL paths with reasonable depth
            lambda p: p.count('/') >= 2 and not p.endswith('/')
        ]
        
        # Check if any indicator matches
        is_article = False
        for indicator in article_indicators:
            if callable(indicator):
                if indicator(path):
                    is_article = True
                    break
            elif indicator in full_url:
                is_article = True
                break
            elif isinstance(indicator, str) and indicator.startswith('r') and re.search(indicator, full_url):
                is_article = True
                break
                
        # Add article URL if it looks like an article
        if is_article:
            article_urls.append(full_url)
    
    # Remove duplicates while preserving order
    seen = set()
    article_urls = [x for x in article_urls if not (x in seen or seen.add(x))]
    
    return article_urls


def fetch_content_with_selectors(url, selectors):
    """
    Fetch content from original website using custom selectors
    
    Args:
        url: URL to fetch
        selectors: Dictionary of CSS selectors
    
    Returns:
        str: Extracted content or empty string if failed
    """
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'lxml')
        
        # Use content selector if available
        content_selector = selectors.get('content')
        if content_selector:
            content_element = soup.select_one(content_selector)
            if content_element:
                return str(content_element)
        
        # Fallback to article selector
        article_selector = selectors.get('article')
        if article_selector:
            article_element = soup.select_one(article_selector)
            if article_element:
                return str(article_element)
                
        return ""
    except Exception as e:
        current_app.logger.error(f"Error fetching content with selectors from {url}: {str(e)}")
        return ""


def calculate_quality_score(item_count, avg_content_length, image_ratio):
    """Calculate a quality score from 0-100 based on various metrics"""
    # Base score
    score = 50
    
    # Item count score (0-20)
    if item_count >= 10:
        score += 20
    elif item_count >= 5:
        score += 10
    elif item_count >= 1:
        score += 5
    
    # Content length score (0-50)
    if avg_content_length >= 1000:
        score += 30
    elif avg_content_length >= 500:
        score += 20
    elif avg_content_length >= 200:
        score += 10
    
    # Image ratio score (0-20)
    if image_ratio >= 0.8:
        score += 20
    elif image_ratio >= 0.5:
        score += 15
    elif image_ratio >= 0.3:
        score += 10
    elif image_ratio > 0:
        score += 5
    
    # Cap score at 100
    return min(score, 100)


def create_alert(feed_source_id, level, message):
    """Create a system alert"""
    alert = Alert(
        feed_source_id=feed_source_id,
        level=level,
        message=message
    )
    db.session.add(alert)
    # Don't commit here, let the caller commit


def validate_rsshub_route(route):
    """Validate if a RSSHub route exists and returns valid RSS"""
    # Special case for custom routes
    if route.startswith('custom/'):
        return True, "Custom route format is valid"
        
    rsshub_base_url = current_app.config.get('RSSHUB_BASE_URL')
    if not rsshub_base_url:
        return False, "RSSHub base URL not configured"
    
    # Remove leading slash if present
    route = route.lstrip('/')
    full_url = urljoin(rsshub_base_url, route)
    
    try:
        response = requests.get(full_url, timeout=30)
        response.raise_for_status()
        
        # Try to parse as RSS
        feed = feedparser.parse(response.content)
        
        if not hasattr(feed, 'feed') or not hasattr(feed, 'entries'):
            return False, "Invalid RSS feed format"
        
        if not feed.entries:
            return True, "Feed is valid but contains no items"
        
        return True, f"Valid feed with {len(feed.entries)} items"
        
    except requests.exceptions.RequestException as e:
        return False, f"Request error: {str(e)}"
    except Exception as e:
        return False, f"Parse error: {str(e)}"


def check_all_feeds():
    """Check all active feeds and update their status"""
    sources = FeedSource.query.filter_by(is_active=True).all()
    
    for source in sources:
        fetch_and_parse_feed(source)
        
    return len(sources)


def get_feed_health(feed_source_id, days=7):
    """Get feed health metrics for the given period"""
    logs = FetchLog.query.filter_by(
        feed_source_id=feed_source_id
    ).order_by(FetchLog.fetched_at.desc()).limit(days).all()
    
    success_rate = sum(1 for log in logs if log.status == 'success') / len(logs) if logs else 0
    avg_quality = sum(log.quality_score for log in logs if log.quality_score) / len(logs) if logs else 0
    avg_items = sum(log.item_count for log in logs) / len(logs) if logs else 0
    
    return {
        'success_rate': success_rate,
        'avg_quality': avg_quality,
        'avg_items': avg_items,
        'total_checks': len(logs)
    }


def get_feed_preview(rsshub_route, max_items=3):
    """Get a preview of feed items without saving to database"""
    # Handle custom routes with newspaper3k
    if rsshub_route.startswith('custom/'):
        try:
            # Extract domain from route
            domain = rsshub_route.replace('custom/', '')
            url = f"https://{domain.replace('-', '.')}"
            
            # Create sample preview with newspaper3k
            article = Article(url)
            article.download()
            article.parse()
            
            # If no content found, try to build a source and get top articles
            if not article.text:
                source = build(url, memoize_articles=False)
                source.download()
                source.parse()
                
                # Get articles
                preview_items = []
                for article_url in source.article_urls()[:max_items]:
                    try:
                        preview_article = Article(article_url)
                        preview_article.download()
                        preview_article.parse()
                        
                        text_content = preview_article.text or ''
                        
                        preview_items.append({
                            'title': preview_article.title,
                            'link': preview_article.url,
                            'description': preview_article.meta_description or text_content[:200],
                            'content': preview_article.article_html or f"<p>{text_content}</p>",
                            'text_content': text_content[:500] + '...' if len(text_content) > 500 else text_content,
                            'image_url': preview_article.top_image,
                            'published': preview_article.publish_date,
                            'word_count': len(text_content.split()) if text_content else 0,
                        })
                    except Exception as e:
                        logger.error(f"Error creating preview for {article_url}: {str(e)}")
                
                return preview_items or None
            
            # Return single article preview
            text_content = article.text or ''
            return [{
                'title': article.title,
                'link': article.url,
                'description': article.meta_description or text_content[:200],
                'content': article.article_html or f"<p>{text_content}</p>",
                'text_content': text_content[:500] + '...' if len(text_content) > 500 else text_content,
                'image_url': article.top_image,
                'published': article.publish_date,
                'word_count': len(text_content.split()) if text_content else 0,
            }]
        except Exception as e:
            logger.error(f"Error previewing custom route {rsshub_route}: {str(e)}")
            return None
            
    # Standard RSSHub routes (unchanged)
    rsshub_base_url = current_app.config.get('RSSHUB_BASE_URL')
    if not rsshub_base_url:
        return None
    
    # Remove leading slash if present
    route = rsshub_route.lstrip('/')
    full_url = urljoin(rsshub_base_url, route)
    
    try:
        response = requests.get(full_url, timeout=30)
        response.raise_for_status()
        
        # Parse the feed
        feed_data = feedparser.parse(response.content)
        
        preview_items = []
        for entry in feed_data.entries[:max_items]:
            # Extract content with fallbacks
            content = ''
            if hasattr(entry, 'content'):
                content = entry.content[0].value
            elif hasattr(entry, 'content_encoded'):
                content = entry.content_encoded
            elif hasattr(entry, 'description'):
                content = entry.description
            elif hasattr(entry, 'summary'):
                content = entry.summary
            
            # Find image with multiple fallbacks
            image_url = None
            
            # Method 1: Check media_content
            if hasattr(entry, 'media_content') and entry.media_content:
                for media in entry.media_content:
                    if 'url' in media and (
                        'image' in media.get('type', '') or 
                        media.get('url', '').lower().endswith(('.jpg', '.jpeg', '.png', '.gif'))
                    ):
                        image_url = media.get('url')
                        break
            
            # Method 2: Check enclosures
            if not image_url and hasattr(entry, 'enclosures') and entry.enclosures:
                for enclosure in entry.enclosures:
                    if 'type' in enclosure and 'image' in enclosure.get('type', ''):
                        image_url = enclosure.get('href', None) or enclosure.get('url', None)
                        if image_url:
                            break
            
            # Method 3: Extract from content HTML
            if not image_url and content:
                soup = BeautifulSoup(content, 'lxml')
                img_tag = soup.find('img')
                if img_tag and img_tag.get('src'):
                    image_url = img_tag.get('src')
            
            # Method 4: Try to get from media_thumbnail
            if not image_url and hasattr(entry, 'media_thumbnail') and entry.media_thumbnail:
                for thumbnail in entry.media_thumbnail:
                    if 'url' in thumbnail:
                        image_url = thumbnail['url']
                        break
            
            # Get text content for display
            text_content = BeautifulSoup(content, 'lxml').get_text() if content else ''
            
            item = {
                'title': getattr(entry, 'title', 'No Title'),
                'link': getattr(entry, 'link', ''),
                'description': getattr(entry, 'summary', ''),
                'content': content,
                'text_content': text_content[:500] + '...' if len(text_content) > 500 else text_content,
                'image_url': image_url,
                'published': getattr(entry, 'published', None),
                'word_count': len(re.findall(r'\w+', text_content)) if text_content else 0,
            }
            preview_items.append(item)
        
        return preview_items
        
    except Exception as e:
        current_app.logger.error(f"Error previewing feed {rsshub_route}: {str(e)}")
        return None

def suggest_selectors(url):
    """
    Simplified selector suggestion - with newspaper3k, we don't really need selectors anymore,
    but we'll return a simple structure to maintain API compatibility.
    
    Args:
        url: URL to analyze
    
    Returns:
        dict: Suggested selectors (minimal as newspaper3k handles extraction)
    """
    try:
        # Try to extract content with newspaper3k
        article = Article(url)
        article.download()
        article.parse()
        
        # If successful, just return basic selectors
        # (these won't actually be used by the newspaper implementation
        # but we return something to maintain API compatibility)
        return {
            'content': 'article',
            'title': 'h1',
            'date': 'time'
        }
    except Exception as e:
        current_app.logger.error(f"Error analyzing URL with newspaper3k: {str(e)}")
        return {}