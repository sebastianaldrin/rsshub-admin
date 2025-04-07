import feedparser
import requests
from bs4 import BeautifulSoup
import json
import re
import time
from datetime import datetime
from urllib.parse import urlparse, urljoin
from flask import current_app
from models import db, FeedSource, FetchLog, FeedItem, Alert


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
        # This is a custom route, use direct scraping
        url = feed_source.original_url
        if not url:
            return 'error', 'Original URL is required for custom routes', None, 0
        
        try:
            # Parse custom selectors
            custom_selectors = {}
            if feed_source.custom_selectors:
                try:
                    custom_selectors = json.loads(feed_source.custom_selectors)
                except json.JSONDecodeError:
                    return 'error', 'Invalid custom selectors JSON', None, 0
            
            if not custom_selectors:
                return 'error', 'Custom selectors are required for custom routes', None, 0
            
            # Fetch the page
            response = requests.get(url, timeout=30, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            })
            response.raise_for_status()
            
            # Parse the page
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # First determine if this is likely an article page or a listing/homepage
            is_article = is_article_page(soup, url)
            
            # Process based on page type
            feed_items = []
            
            if is_article:
                # This is a single article page
                item = extract_article_content(soup, url, custom_selectors)
                if item:
                    feed_items = [item]
            else:
                # This is a listing/homepage - find and process article links
                article_urls = find_article_links(soup, url, custom_selectors)
                
                # Process each article
                for article_url in article_urls[:10]:  # Limit to 10 articles for performance
                    try:
                        # Fetch the article page
                        article_response = requests.get(article_url, timeout=30, headers={
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                        })
                        article_response.raise_for_status()
                        
                        # Parse the article
                        article_soup = BeautifulSoup(article_response.content, 'html.parser')
                        
                        # Extract article content
                        item = extract_article_content(article_soup, article_url, custom_selectors)
                        if item:
                            feed_items.append(item)
                    except Exception as e:
                        current_app.logger.error(f"Error processing article {article_url}: {str(e)}")
                        continue
            
            # If no items could be extracted, return an error
            if not feed_items:
                return 'error', 'No items could be extracted from the website', None, 0
            
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
                        published_at=item.get('published_at', datetime.utcnow()),
                        has_full_content=item.get('has_full_content', False),
                        word_count=item.get('word_count', 0),
                        quality_issues=item.get('quality_issues'),
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
            error_msg = str(e)
            
            # Create error log
            fetch_log = FetchLog(
                feed_source_id=feed_source.id,
                status='error',
                error_message=error_msg,
                fetch_duration=time.time() - start_time
            )
            db.session.add(fetch_log)
            
            # Create alert
            create_alert(
                feed_source.id, 
                'error', 
                f"Failed to process custom route: {feed_source.name} - {error_msg}"
            )
            
            db.session.commit()
            return 'error', error_msg, None, 0
    
    # Construct the full RSSHub URL
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
                    published_at=published_at,
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


def is_article_page(soup, url):
    """
    Determine if a page is likely an article page or a listing/homepage.
    
    Args:
        soup: BeautifulSoup object
        url: URL of the page
        
    Returns:
        bool: True if it's likely an article page, False if it's likely a listing/homepage
    """
    # Check URL patterns common for articles
    url_indicators = ['article', 'story', 'news', 'post', 'read', '/a/', '/s/']
    if any(indicator in url.lower() for indicator in url_indicators):
        return True
    
    # Check for article elements
    article_indicators = [
        soup.find('article'),
        soup.find(class_=re.compile(r'article|story|post')),
        soup.find(id=re.compile(r'article|story|post')),
        soup.find(attrs={"itemprop": "articleBody"}),
        soup.find(attrs={"property": "articleBody"}),
        soup.find('meta', property="og:type", content="article")
    ]
    
    if any(indicator for indicator in article_indicators):
        return True
        
    # Check if there's a single prominent h1
    h1_tags = soup.find_all('h1')
    if len(h1_tags) == 1 and len(h1_tags[0].get_text(strip=True)) > 20:
        return True
        
    # Count links - homepages tend to have many more links
    links = soup.find_all('a', href=True)
    if len(links) < 20:  # Fewer links suggest an article
        return True
        
    # Default to homepage/listing
    return False


def find_article_links(soup, base_url, selectors):
    """
    Find article links on a homepage or listing page.
    
    Args:
        soup: BeautifulSoup object
        base_url: Base URL for resolving relative links
        selectors: Dictionary of custom selectors
        
    Returns:
        list: List of article URLs
    """
    article_urls = []
    
    # Try to use custom link selector if provided
    if selectors and 'link' in selectors and selectors['link']:
        link_elements = soup.select(selectors['link'])
        for element in link_elements:
            # If the element is already an <a> tag
            if element.name == 'a' and element.has_attr('href'):
                article_urls.append(urljoin(base_url, element['href']))
            else:
                # Otherwise look for links inside the element
                links = element.select('a[href]')
                for link in links:
                    article_urls.append(urljoin(base_url, link['href']))
    
    # If no links found or no custom selector, try common patterns
    if not article_urls:
        # Try common article container + link patterns
        article_containers = [
            # List items or cards
            ('li.article', 'a'),
            ('.article', 'a'),
            ('.post', 'a'),
            ('.card', 'a'),
            ('.news-item', 'a'),
            ('.story', 'a'),
            ('.item', 'a'),
            
            # Heading links
            ('h2', 'a'),
            ('h3', 'a'),
            ('.headline', 'a'),
            ('.title', 'a'),
            
            # Direct link selectors
            ('a.article-link', None),
            ('a.headline', None),
            ('a.title', None)
        ]
        
        for container, link_selector in article_containers:
            if link_selector:
                # Two-level selection (container -> link)
                containers = soup.select(container)
                for item in containers:
                    links = item.select(link_selector)
                    for link in links:
                        if link.has_attr('href'):
                            article_urls.append(urljoin(base_url, link['href']))
            else:
                # Direct link selection
                links = soup.select(container)
                for link in links:
                    if link.has_attr('href'):
                        article_urls.append(urljoin(base_url, link['href']))
    
    # If still no links, try to find any promising links
    if not article_urls:
        # Look for links with article-like URL patterns
        all_links = soup.find_all('a', href=True)
        for link in all_links:
            href = link['href']
            # Skip navigation, social, and other non-article links
            if any(x in href.lower() for x in ['javascript:', 'mailto:', '#', 'facebook', 'twitter', 'instagram']):
                continue
                
            # Look for article-like URL patterns
            if any(x in href.lower() for x in ['article', 'story', 'news', 'post', '/a/', '/s/']):
                article_urls.append(urljoin(base_url, href))
    
    # Remove duplicates while preserving order
    seen = set()
    article_urls = [x for x in article_urls if not (x in seen or seen.add(x))]
    
    return article_urls


def extract_article_content(soup, url, selectors):
    """
    Extract content from an article page using custom selectors.
    
    Args:
        soup: BeautifulSoup object
        url: URL of the article
        selectors: Dictionary of custom selectors
        
    Returns:
        dict: Article data or None if extraction failed
    """
    try:
        # Extract title
        title = "Untitled"
        if 'title' in selectors and selectors['title']:
            title_element = soup.select_one(selectors['title'])
            if title_element:
                title = title_element.get_text(strip=True)
        
        # If no title found, try to use the page title
        if not title or title == "Untitled":
            if soup.title:
                title = soup.title.string.strip()
            else:
                # Try common title patterns
                for title_selector in ['h1', '.headline', '.article-title', '[itemprop="headline"]']:
                    title_element = soup.select_one(title_selector)
                    if title_element:
                        title = title_element.get_text(strip=True)
                        break
        
        # Extract content
        content = ""
        if 'content' in selectors and selectors['content']:
            content_element = soup.select_one(selectors['content'])
            if content_element:
                # Preserve the HTML structure
                content = str(content_element)
                
                # Count text for metrics
                text_content = content_element.get_text(strip=True)
                word_count = len(re.findall(r'\w+', text_content))
                
                # Check if content is too short
                if len(text_content) < 50:
                    current_app.logger.warning(f"Content too short for {url}: {len(text_content)} chars")
        
        # If no content found, try common content selectors
        if not content:
            for content_selector in ['article', '.article-body', '.post-content', '[itemprop="articleBody"]']:
                content_element = soup.select_one(content_selector)
                if content_element:
                    content = str(content_element)
                    text_content = content_element.get_text(strip=True)
                    word_count = len(re.findall(r'\w+', text_content))
                    break
        
        # If still no content, try paragraphs directly
        if not content:
            main_element = soup.find('main') or soup.find(id='content') or soup.find(class_='content') or soup.body
            if main_element:
                paragraphs = main_element.find_all('p')
                if paragraphs:
                    content = ''.join(str(p) for p in paragraphs[:15])  # Take first 15 paragraphs
                    text_content = ' '.join(p.get_text(strip=True) for p in paragraphs[:15])
                    word_count = len(re.findall(r'\w+', text_content))
        
        # If no content could be extracted, return None
        if not content:
            return None
        
        # Extract date
        published_at = None
        if 'date' in selectors and selectors['date']:
            date_element = soup.select_one(selectors['date'])
            if date_element:
                # Try datetime attribute first
                date_str = date_element.get('datetime')
                if not date_str:
                    date_str = date_element.get_text(strip=True)
                
                if date_str:
                    published_at = parse_date(date_str)
        
        # If no date found with selector, try common patterns
        if not published_at:
            for date_selector in ['[itemprop="datePublished"]', 'time', '.date', '.published']:
                date_element = soup.select_one(date_selector)
                if date_element:
                    # Try datetime attribute first
                    date_str = date_element.get('datetime')
                    if not date_str:
                        date_str = date_element.get_text(strip=True)
                    
                    if date_str:
                        published_at = parse_date(date_str)
                        break
        
        # If still no date found, use current time
        if not published_at:
            published_at = datetime.utcnow()
        
        # Extract image
        image_url = None
        
        # Try to find a featured image first
        featured_image_selectors = ['.featured-image img', '.hero-image img', '[itemprop="image"]', '.article-image img']
        for img_selector in featured_image_selectors:
            img = soup.select_one(img_selector)
            if img and img.has_attr('src'):
                image_url = urljoin(url, img['src'])
                break
        
        # If no featured image, look in the content
        if not image_url and content:
            content_soup = BeautifulSoup(content, 'html.parser')
            img_tags = content_soup.find_all('img', src=True)
            if img_tags:
                # Get first image with reasonable size
                for img in img_tags:
                    if img.get('src'):
                        image_url = urljoin(url, img['src'])
                        break
        
        # Extract author
        author = extract_author(soup)
        
        # Generate a unique identifier
        guid = url
        
        # Create article data
        article = {
            'title': title,
            'link': url,
            'guid': guid,
            'description': text_content[:200] + '...' if len(text_content) > 200 else text_content,
            'content': content,
            'author': author,
            'image_url': image_url,
            'published_at': published_at,
            'has_full_content': True,
            'word_count': word_count if 'word_count' in locals() else 0,
            'quality_issues': json.dumps([]) if content and title else json.dumps(["incomplete_content"]),
            'extraction_metadata': json.dumps({
                'extraction_method': 'custom_selectors',
                'content_length': len(content) if content else 0
            })
        }
        
        return article
    
    except Exception as e:
        current_app.logger.error(f"Error extracting content from {url}: {str(e)}")
        return None


def extract_author(soup):
    """Try to extract the author from common patterns."""
    author_selectors = [
        '[rel="author"]',
        '[itemprop="author"]',
        '.author',
        '.byline',
        'meta[name="author"]'
    ]
    
    for selector in author_selectors:
        author_element = soup.select_one(selector)
        if author_element:
            if author_element.name == 'meta' and author_element.get('content'):
                return author_element['content']
            return author_element.get_text(strip=True)
    
    return None


def parse_date(date_str):
    """
    Try to parse a date string in various formats.
    
    Args:
        date_str: Date string to parse
        
    Returns:
        datetime or None
    """
    from datetime import datetime, timedelta
    
    # Common date formats
    formats = [
        '%Y-%m-%dT%H:%M:%S%z',  # ISO 8601 with timezone
        '%Y-%m-%dT%H:%M:%S.%f%z',  # ISO 8601 with microseconds and timezone
        '%Y-%m-%dT%H:%M:%S',  # ISO 8601 without timezone
        '%Y-%m-%d %H:%M:%S',  # Standard datetime
        '%Y-%m-%d',  # Just date
        '%B %d, %Y',  # January 1, 2023
        '%d %B %Y',  # 1 January 2023
        '%m/%d/%Y',  # MM/DD/YYYY
        '%d/%m/%Y',  # DD/MM/YYYY
    ]
    
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    
    # Try more complex parsing for relative dates
    try:
        if 'ago' in date_str.lower():
            # Handle "X time ago" format
            value = int(re.search(r'\d+', date_str).group())
            if 'minute' in date_str.lower():
                return datetime.utcnow().replace(microsecond=0) - timedelta(minutes=value)
            elif 'hour' in date_str.lower():
                return datetime.utcnow().replace(microsecond=0) - timedelta(hours=value)
            elif 'day' in date_str.lower():
                return datetime.utcnow().replace(microsecond=0) - timedelta(days=value)
            elif 'week' in date_str.lower():
                return datetime.utcnow().replace(microsecond=0) - timedelta(weeks=value)
            elif 'month' in date_str.lower():
                return datetime.utcnow().replace(microsecond=0) - timedelta(days=value*30)
    except:
        pass
    
    return datetime.utcnow()


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
    # Handle custom routes
    if rsshub_route.startswith('custom/'):
        return None  # Previews for custom routes not implemented yet
        
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


def generate_rsshub_route(site_info):
    """
    Generate RSSHub route based on site information
    
    Args:
        site_info: Dictionary with site information including type and parameters
    
    Returns:
        str: Generated RSSHub route
    """
    site_type = site_info.get('type', '')
    
    if site_type == 'generic':
        return f"rsshub/radar?url={site_info.get('url', '')}"
    
    elif site_type == 'reddit':
        subreddit = site_info.get('subreddit', '')
        sort = site_info.get('sort', '')
        if sort:
            return f"reddit/r/{subreddit}/{sort}"
        return f"reddit/r/{subreddit}"
    
    elif site_type == 'twitter':
        username = site_info.get('username', '')
        return f"twitter/user/{username}"
    
    elif site_type == 'youtube':
        channel = site_info.get('channel', '')
        return f"youtube/channel/{channel}"
    
    elif site_type == 'github':
        username = site_info.get('username', '')
        repo = site_info.get('repo', '')
        if repo:
            return f"github/repos/{username}/{repo}/releases"
        return f"github/repos/{username}"
    
    return ""


def suggest_selectors(url):
    """
    Suggest selectors for a given URL by analyzing page structure
    
    Args:
        url: URL to analyze
    
    Returns:
        dict: Suggested selectors
    """
    try:
        # Fetch the page with a proper user agent
        response = requests.get(url, timeout=30, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Determine if this is an article page or a listing/homepage
        is_article = is_article_page(soup, url)
        
        selectors = {}
        
        if is_article:
            # This is an article page - suggest content, title, and date selectors
            
            # Try to find article content
            potential_content_selectors = [
                'article', 
                '.article', 
                '.post', 
                '.entry', 
                '.content',
                '#content',
                '[itemprop="articleBody"]',
                '.article-content',
                '.post-content',
                '.entry-content',
                '.story-body',
                '.article-body',
                '.main-content'
            ]
            
            # Score content selectors by text length
            scored_content_selectors = []
            for selector in potential_content_selectors:
                elements = soup.select(selector)
                for element in elements:
                    text_length = len(element.get_text(strip=True))
                    if text_length > 200:  # Only consider elements with substantial text
                        scored_content_selectors.append((selector, text_length))
            
            # Sort by text length (descending)
            scored_content_selectors.sort(key=lambda x: x[1], reverse=True)
            
            # Use the selector with the most text
            if scored_content_selectors:
                selectors['content'] = scored_content_selectors[0][0]
            
            # Try to find title
            potential_title_selectors = [
                'h1',
                '.title',
                '.headline',
                '[itemprop="headline"]',
                '.article-title',
                '.post-title'
            ]
            
            # Score title selectors by whether they're within a likely article container
            scored_title_selectors = []
            for selector in potential_title_selectors:
                elements = soup.select(selector)
                for element in elements:
                    # Title should be reasonably long but not too long
                    text = element.get_text(strip=True)
                    if 10 <= len(text) <= 200:
                        # Check if in article container
                        in_article = False
                        for parent in element.parents:
                            if parent.name == 'article' or (parent.get('class') and any('article' in c.lower() for c in parent.get('class'))):
                                in_article = True
                                break
                        
                        # Score: length + being in article + having appropriate H-level
                        score = len(text) + (50 if in_article else 0) + (100 if element.name == 'h1' else 0)
                        scored_title_selectors.append((selector, score))
            
            # Sort by score (descending)
            scored_title_selectors.sort(key=lambda x: x[1], reverse=True)
            
            # Use the highest scoring title selector
            if scored_title_selectors:
                selectors['title'] = scored_title_selectors[0][0]
            
            # Try to find date
            potential_date_selectors = [
                '[itemprop="datePublished"]',
                '[datetime]',
                'time',
                '.date',
                '.published',
                '.timestamp',
                '.article-date',
                '.meta-date'
            ]
            
            # First try to find elements with datetime attribute
            for selector in potential_date_selectors:
                elements = soup.select(selector)
                for element in elements:
                    if element.has_attr('datetime') or (element.name == 'time'):
                        selectors['date'] = selector
                        break
                if 'date' in selectors:
                    break
            
            # If no date found, look for elements with date-like text
            if 'date' not in selectors:
                date_pattern = re.compile(r'\d{1,4}[\/\.-]\d{1,2}[\/\.-]\d{1,4}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}', re.I)
                
                for el in soup.find_all(['span', 'div', 'p', 'time']):
                    text = el.get_text(strip=True)
                    if date_pattern.search(text):
                        # Try to generate a selector for this element
                        if el.get('class'):
                            selectors['date'] = '.' + '.'.join(el.get('class'))
                            break
                        elif el.get('id'):
                            selectors['date'] = '#' + el.get('id')
                            break
                        elif el.name == 'time':
                            selectors['date'] = 'time'
                            break
        else:
            # This is a listing/homepage - suggest list item and link selectors
            
            # Find article listing containers
            potential_list_selectors = [
                'article',
                '.article',
                '.post',
                '.entry',
                '.card',
                '.news-item',
                '.list-item',
                'li.article',
                '.article-card'
            ]
            
            # Score based on number of similar elements and link presence
            scored_list_selectors = []
            for selector in potential_list_selectors:
                elements = soup.select(selector)
                if len(elements) >= 3:  # At least 3 similar elements
                    # Check if these elements contain links
                    link_count = sum(1 for el in elements if el.select('a[href]'))
                    if link_count >= 3:
                        scored_list_selectors.append((selector, link_count))
            
            # Use the list selector with the most links
            if scored_list_selectors:
                scored_list_selectors.sort(key=lambda x: x[1], reverse=True)
                selectors['list_item'] = scored_list_selectors[0][0]
                
                # Now try to find a link selector within the list items
                list_items = soup.select(selectors['list_item'])
                
                # Collect all links in first 3 list items
                all_links = []
                for item in list_items[:3]:
                    item_links = item.select('a[href]')
                    all_links.extend(item_links)
                
                # Try to identify a pattern for headline links
                if all_links:
                    # Try links in headings first
                    heading_links = [link for link in all_links if link.parent.name in ['h1', 'h2', 'h3', 'h4']]
                    if heading_links:
                        # Use heading > a pattern
                        selectors['link'] = selectors['list_item'] + ' ' + heading_links[0].parent.name + ' a'
                    else:
                        # Use class-based pattern if available
                        classed_links = [link for link in all_links if link.get('class')]
                        if classed_links:
                            selectors['link'] = 'a.' + '.'.join(classed_links[0].get('class'))
                        else:
                            # Default to any link in the list item
                            selectors['link'] = selectors['list_item'] + ' a'
            
            # If no list selector found, try to find a common link pattern
            if 'list_item' not in selectors:
                # Look for groups of similar links
                link_patterns = {}
                
                # Check links inside headings
                for tag in ['h2', 'h3']:
                    links = soup.select(f'{tag} a')
                    if len(links) >= 3:
                        link_patterns[f'{tag} a'] = len(links)
                
                # Check links with specific classes
                for link in soup.select('a[href]'):
                    if link.get('class'):
                        cls = '.'.join(link.get('class'))
                        selector = f'a.{cls}'
                        if selector in link_patterns:
                            link_patterns[selector] += 1
                        else:
                            link_patterns[selector] = 1
                
                # Use the pattern with the most occurrences
                if link_patterns:
                    best_pattern = max(link_patterns.items(), key=lambda x: x[1])
                    if best_pattern[1] >= 3:  # At least 3 occurrences
                        selectors['link'] = best_pattern[0]
        
        return selectors
    
    except Exception as e:
        current_app.logger.error(f"Error suggesting selectors for {url}: {str(e)}")
        return {}