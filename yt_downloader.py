from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import yt_dlp
import os
import json
import threading
import uuid
import re
import time
import hashlib
import logging
import signal
from datetime import datetime, timedelta
from functools import lru_cache
from threading import Lock
from logging.handlers import RotatingFileHandler

app = Flask(__name__)
CORS(app)

# ============ LOGGING SETUP ============
def setup_logging():
    """Production ready logging"""
    logger = logging.getLogger('yt_downloader')
    logger.setLevel(logging.INFO)
    
    # Format log
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # File handler (rotating, max 10MB, 5 backups)
    fh = RotatingFileHandler('/tmp/yt-downloader.log', maxBytes=10485760, backupCount=5)
    fh.setLevel(logging.INFO)
    fh.setFormatter(formatter)
    
    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    
    logger.addHandler(fh)
    logger.addHandler(ch)
    
    return logger

logger = setup_logging()
# =======================================

# ============ KONFIGURASI ============
DOWNLOAD_PATH = "/tmp/yt-downloads/"
HISTORY_FILE = "/tmp/history.json"
LOCK_FILE = "/tmp/history.lock"
os.makedirs(DOWNLOAD_PATH, exist_ok=True)

# Constants
MAX_HISTORY_ITEMS = 25
MAX_FORMATS = 10
AUDIO_QUALITY = '192'
CACHE_TIMEOUT = 300
CLEANUP_INTERVAL = 300
DOWNLOAD_EXPIRY = 3600
MAX_CACHE_SIZE = 100
DOWNLOAD_TIMEOUT = 600
MAX_RETRIES = 2  # yt-dlp udah punya retry internal
REQUEST_TIMEOUT = 30
CLEANUP_GRACE_PERIOD = 30  # 30 detik buat cleanup sebelum exit
# =====================================

# ============ LOCKS ============
downloads_lock = Lock()
cache_lock = Lock()
history_lock = Lock()
cleanup_lock = Lock()
# ================================

# ============ STORAGE ============
active_downloads = {}
video_cache = {}
cleanup_thread = None
shutdown_event = threading.Event()
# ================================

# ============ RATE LIMITING ============
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
    strategy="fixed-window"
)
# =====================================

# ============ VALIDASI SUPER KETAT ============
def validate_youtube_url(url):
    """Validasi URL YouTube - Anti bypass"""
    if not url or not isinstance(url, str):
        return False
    
    url = url.strip()
    
    # Pattern lengkap + anti bypass
    patterns = [
        # Standard watch
        r'^https?://(?:www\.)?youtube\.com/watch\?v=([\w-]{11})(?:&\S*)?$',
        # Shorts
        r'^https?://(?:www\.)?youtube\.com/shorts/([\w-]{11})(?:\?\S*)?$',
        # YouTu.be
        r'^https?://(?:www\.)?youtu\.be/([\w-]{11})(?:\?\S*)?$',
        # Embed
        r'^https?://(?:www\.)?youtube\.com/embed/([\w-]{11})(?:\?\S*)?$',
        # Mobile
        r'^https?://(?:www\.)?m\.youtube\.com/watch\?v=([\w-]{11})(?:&\S*)?$',
        # Music
        r'^https?://(?:www\.)?music\.youtube\.com/watch\?v=([\w-]{11})(?:&\S*)?$'
    ]
    
    for pattern in patterns:
        match = re.match(pattern, url)
        if match:
            # Extract video ID
            video_id = match.group(1)
            # Validasi video ID format
            if re.match(r'^[\w-]{11}$', video_id):
                return True
    
    return False

def sanitize_filename(filename):
    """Bersihin filename dari karakter jahat"""
    if not filename:
        return f"video_{uuid.uuid4().hex[:8]}.mp4"
    
    # Hapus karakter berbahaya
    filename = re.sub(r'[^\w\-_.\s]', '', filename)
    filename = re.sub(r'\.{2,}', '.', filename)
    filename = filename.strip('. ')
    filename = filename[:100]
    
    return filename or f"video_{uuid.uuid4().hex[:8]}.mp4"

def validate_download_id(download_id):
    """Validasi UUID format"""
    if not download_id or not isinstance(download_id, str):
        return False
    return bool(re.match(r'^[a-f0-9-]{36}$', download_id.lower()))
# ===============================================

# ============ ATOMIC HISTORY WRITE ============
def atomic_write_history(history):
    """Anti corruption - write ke temp file dulu"""
    temp_file = f"{HISTORY_FILE}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temp_file, 'w') as f:
            json.dump(history, f, indent=2)
        os.replace(temp_file, HISTORY_FILE)  # Atomic operation
        return True
    except Exception as e:
        logger.error(f"Gagal save history: {e}")
        if os.path.exists(temp_file):
            os.remove(temp_file)
        return False

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.error("History file corrupted, creating new")
            # Backup corrupted file
            backup = f"{HISTORY_FILE}.corrupted.{datetime.now().timestamp()}"
            os.rename(HISTORY_FILE, backup)
            return []
        except Exception as e:
            logger.error(f"Gagal load history: {e}")
            return []
    return []

def save_history(history):
    with history_lock:
        return atomic_write_history(history)
# =============================================

# ============ THREAD SAFE OPERATIONS ============
def get_download(download_id):
    with downloads_lock:
        return active_downloads.get(download_id)

def set_download(download_id, data):
    with downloads_lock:
        active_downloads[download_id] = data

def update_download(download_id, **kwargs):
    with downloads_lock:
        if download_id in active_downloads:
            active_downloads[download_id].update(kwargs)
            return True
    return False

def delete_download(download_id):
    with downloads_lock:
        return active_downloads.pop(download_id, None)

def get_cache_key(url):
    return hashlib.md5(url.encode()).hexdigest()

def get_cached_video(url):
    cache_key = get_cache_key(url)
    with cache_lock:
        if cache_key in video_cache:
            cached = video_cache[cache_key]
            if datetime.now() < cached['expires']:
                cached['hits'] = cached.get('hits', 0) + 1
                cached['last_access'] = datetime.now()
                return cached['data']
    return None

def set_cached_video(url, data):
    cache_key = get_cache_key(url)
    with cache_lock:
        # Enforce max cache size - LRU style
        if len(video_cache) >= MAX_CACHE_SIZE:
            # Hapus yang paling jarang dipake (LRU)
            oldest = min(
                video_cache.items(),
                key=lambda x: (
                    x[1].get('hits', 0),
                    x[1].get('last_access', x[1].get('created', datetime.min))
                )
            )
            del video_cache[oldest[0]]
            logger.debug(f"Cache full, removed: {oldest[0]}")
        
        video_cache[cache_key] = {
            'data': data,
            'expires': datetime.now() + timedelta(seconds=CACHE_TIMEOUT),
            'created': datetime.now(),
            'last_access': datetime.now(),
            'hits': 0
        }

def clean_expired_cache():
    removed = 0
    with cache_lock:
        expired = []
        now = datetime.now()
        for key, cached in video_cache.items():
            if now > cached['expires']:
                expired.append(key)
        for key in expired:
            del video_cache[key]
            removed += 1
    return removed
# ===============================================

# ============ CLEANUP THREAD (GRACEFUL SHUTDOWN) ============
class CleanupThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = False
        self.name = "CleanupThread"
        self.running = True
        self.shutdown_event = shutdown_event
    
    def run(self):
        logger.info("🧹 Cleanup thread started")
        while self.running and not self.shutdown_event.is_set():
            try:
                self.cleanup()
                # Wait with shutdown support
                self.shutdown_event.wait(CLEANUP_INTERVAL)
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
                self.shutdown_event.wait(60)
        
        # Final cleanup before exit
        logger.info("🧹 Cleanup thread shutting down, final cleanup...")
        self.cleanup(force=True)
        logger.info("🧹 Cleanup thread stopped")
    
    def cleanup(self, force=False):
        now = time.time()
        deleted_count = 0
        
        with downloads_lock:
            to_delete = []
            for did, data in active_downloads.items():
                # Force cleanup: hapus semua
                if force:
                    to_delete.append(did)
                    continue
                
                # Normal cleanup
                if data['status'] in ['completed', 'error', 'cancelled']:
                    if 'completed_at' not in data:
                        data['completed_at'] = now
                    elif now - data['completed_at'] > DOWNLOAD_EXPIRY:
                        to_delete.append(did)
            
            # Hapus file fisik
            for did in to_delete:
                data = active_downloads[did]
                if 'filepath' in data and os.path.exists(data['filepath']):
                    try:
                        os.remove(data['filepath'])
                        logger.debug(f"Removed file: {data.get('filename', 'unknown')}")
                    except Exception as e:
                        logger.error(f"Failed to remove file: {e}")
                del active_downloads[did]
                deleted_count += 1
        
        # Bersihin cache
        cache_deleted = clean_expired_cache()
        
        if deleted_count > 0 or cache_deleted > 0:
            logger.info(f"Cleanup: {deleted_count} downloads, {cache_deleted} cache")
    
    def stop(self):
        logger.info("Stopping cleanup thread...")
        self.running = False
        self.shutdown_event.set()

# ============ GRACEFUL SHUTDOWN HANDLER ============
def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}, shutting down...")
    if cleanup_thread:
        cleanup_thread.stop()
        cleanup_thread.join(timeout=CLEANUP_GRACE_PERIOD)
    # Close any open files, connections
    logger.info("Shutdown complete")
    exit(0)

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Start cleanup thread
cleanup_thread = CleanupThread()
cleanup_thread.start()
# ===================================================

# ============ PROGRESS HOOK ============
def progress_hook(download_id):
    def hook(d):
        try:
            if d['status'] == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                downloaded = d.get('downloaded_bytes', 0)
                if total > 0:
                    percent = (downloaded / total) * 100
                    update_download(download_id,
                        progress=round(percent, 1),
                        speed=d.get('speed', 0),
                        eta=d.get('eta', 0)
                    )
            elif d['status'] == 'finished':
                update_download(download_id,
                    progress=100,
                    status='completed',
                    completed_at=time.time()
                )
        except Exception as e:
            logger.error(f"Progress hook error: {e}")
    return hook
# =====================================

# ============ DOWNLOAD DENGAN YT-DLP RETRY ============
def download_with_ytdlp(ydl_opts, url):
    """yt-dlp udah punya retry internal, kita pake itu"""
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=True)
    except Exception as e:
        logger.error(f"Download failed: {e}")
        raise
# ====================================================

# ============ API ENDPOINTS ============
@app.route('/info', methods=['POST'])
@limiter.limit("10 per minute")
def get_info():
    url = request.json.get('url', '').strip()
    
    if not validate_youtube_url(url):
        logger.warning(f"Invalid URL attempt: {url[:50]}...")
        return jsonify({'error': 'URL YouTube gak valid!', 'success': False}), 400
    
    # Cek cache
    cached = get_cached_video(url)
    if cached:
        logger.debug(f"Cache hit: {url[:50]}...")
        return jsonify(cached)
    
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'socket_timeout': REQUEST_TIMEOUT,
            'retries': MAX_RETRIES,  # yt-dlp internal retry
            'fragment_retries': MAX_RETRIES,
            'skip_unavailable_fragments': True,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # Parse formats
            formats = []
            seen = set()
            
            for f in info.get('formats', []):
                height = f.get('height')
                format_note = f.get('format_note', '')
                
                if height:
                    quality = f"{height}p"
                elif format_note:
                    quality = format_note
                else:
                    continue
                
                if quality in seen:
                    continue
                
                if f.get('vcodec') != 'none':
                    seen.add(quality)
                    formats.append({
                        'format_id': f.get('format_id'),
                        'quality': quality,
                        'ext': f.get('ext', 'mp4'),
                        'filesize': f.get('filesize') or f.get('filesize_approx', 0),
                        'has_video': True,
                        'has_audio': f.get('acodec') != 'none'
                    })
            
            # Sort by resolution
            def extract_resolution(q):
                match = re.search(r'(\d+)', q)
                return int(match.group(1)) if match else 0
            
            formats.sort(key=lambda x: extract_resolution(x['quality']), reverse=True)
            
            response_data = {
                'title': info.get('title', 'Unknown'),
                'duration': info.get('duration', 0),
                'channel': info.get('channel', info.get('uploader', 'Unknown')),
                'thumbnail': info.get('thumbnail', ''),
                'formats': formats[:MAX_FORMATS],
                'success': True
            }
            
            # Simpan ke cache
            set_cached_video(url, response_data)
            logger.info(f"Info fetched: {info.get('title', 'Unknown')[:50]}...")
            
            return jsonify(response_data)
            
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp error: {e}")
        return jsonify({'error': 'Gagal ambil info dari YouTube', 'success': False}), 400
    except Exception as e:
        logger.error(f"Info error: {e}")
        return jsonify({'error': str(e), 'success': False}), 400

@app.route('/download', methods=['POST'])
@limiter.limit("3 per minute")
def download_video():
    url = request.json.get('url', '').strip()
    format_id = request.json.get('format_id', 'best')
    
    if not validate_youtube_url(url):
        return jsonify({'error': 'URL YouTube gak valid!', 'success': False}), 400
    
    download_id = str(uuid.uuid4())
    
    set_download(download_id, {
        'progress': 0,
        'status': 'starting',
        'url': url,
        'format': format_id,
        'started_at': time.time(),
        'type': 'video'
    })
    
    def download_task():
        try:
            output_template = os.path.join(
                DOWNLOAD_PATH,
                f'{download_id}_%(title)s.%(ext)s'
            )
            
            ydl_opts = {
                'format': format_id,
                'outtmpl': output_template,
                'progress_hooks': [progress_hook(download_id)],
                'quiet': True,
                'no_warnings': True,
                'ignoreerrors': True,
                'socket_timeout': REQUEST_TIMEOUT,
                'retries': MAX_RETRIES,
                'fragment_retries': MAX_RETRIES,
                'skip_unavailable_fragments': True,
            }
            
            info = download_with_ytdlp(ydl_opts, url)
            
            # Cari file
            expected_pattern = f"{download_id}_"
            found_file = None
            
            for f in os.listdir(DOWNLOAD_PATH):
                if f.startswith(expected_pattern):
                    found_file = f
                    break
            
            if found_file:
                filepath = os.path.join(DOWNLOAD_PATH, found_file)
                
                if os.path.exists(filepath) and os.access(filepath, os.R_OK):
                    update_download(download_id,
                        status='completed',
                        title=info.get('title', 'Unknown'),
                        filepath=filepath,
                        filename=found_file,
                        completed_at=time.time()
                    )
                    
                    # Save history atomically
                    with history_lock:
                        history = load_history()
                        history.insert(0, {
                            'id': str(uuid.uuid4()),
                            'title': info.get('title', 'Unknown'),
                            'url': url,
                            'format': format_id,
                            'type': 'video',
                            'date': datetime.now().isoformat()
                        })
                        save_history(history[:MAX_HISTORY_ITEMS])
                    
                    logger.info(f"Download complete: {info.get('title', 'Unknown')[:50]}...")
                else:
                    raise Exception("File gak bisa dibaca")
                    
        except Exception as e:
            update_download(download_id,
                status='error',
                error=str(e),
                completed_at=time.time()
            )
            logger.error(f"Download error {download_id}: {e}")
    
    thread = threading.Thread(target=download_task, daemon=True)
    thread.name = f"DL-{download_id[:8]}"
    thread.start()
    
    return jsonify({'download_id': download_id, 'success': True})

@app.route('/audio', methods=['POST'])
@limiter.limit("3 per minute")
def download_audio():
    url = request.json.get('url', '').strip()
    
    if not validate_youtube_url(url):
        return jsonify({'error': 'URL YouTube gak valid!', 'success': False}), 400
    
    download_id = str(uuid.uuid4())
    
    set_download(download_id, {
        'progress': 0,
        'status': 'starting',
        'url': url,
        'format': 'mp3',
        'started_at': time.time(),
        'type': 'audio'
    })
    
    def download_task():
        try:
            output_template = os.path.join(
                DOWNLOAD_PATH,
                f'{download_id}_%(title)s.%(ext)s'
            )
            
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': output_template,
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': AUDIO_QUALITY,
                }],
                'progress_hooks': [progress_hook(download_id)],
                'quiet': True,
                'no_warnings': True,
                'ignoreerrors': True,
                'socket_timeout': REQUEST_TIMEOUT,
                'retries': MAX_RETRIES,
                'fragment_retries': MAX_RETRIES,
                'skip_unavailable_fragments': True,
            }
            
            info = download_with_ytdlp(ydl_opts, url)
            
            # Cari file mp3
            expected_pattern = f"{download_id}_"
            found_file = None
            
            for f in os.listdir(DOWNLOAD_PATH):
                if f.startswith(expected_pattern) and f.endswith('.mp3'):
                    found_file = f
                    break
            
            if found_file:
                filepath = os.path.join(DOWNLOAD_PATH, found_file)
                
                if os.path.exists(filepath) and os.access(filepath, os.R_OK):
                    update_download(download_id,
                        status='completed',
                        title=info.get('title', 'Unknown'),
                        filepath=filepath,
                        filename=found_file,
                        completed_at=time.time()
                    )
                    
                    with history_lock:
                        history = load_history()
                        history.insert(0, {
                            'id': str(uuid.uuid4()),
                            'title': info.get('title', 'Unknown'),
                            'url': url,
                            'format': 'mp3',
                            'type': 'audio',
                            'date': datetime.now().isoformat()
                        })
                        save_history(history[:MAX_HISTORY_ITEMS])
                    
                    logger.info(f"Audio complete: {info.get('title', 'Unknown')[:50]}...")
                else:
                    raise Exception("File gak bisa dibaca")
                    
        except Exception as e:
            update_download(download_id,
                status='error',
                error=str(e),
                completed_at=time.time()
            )
            logger.error(f"Audio error {download_id}: {e}")
    
    thread = threading.Thread(target=download_task, daemon=True)
    thread.name = f"AU-{download_id[:8]}"
    thread.start()
    
    return jsonify({'download_id': download_id, 'success': True})

# ============ STATUS ENDPOINTS ============
@app.route('/progress/<download_id>', methods=['GET'])
def get_progress(download_id):
    if not validate_download_id(download_id):
        return jsonify({'error': 'Invalid ID'}), 400
    
    data = get_download(download_id)
    if data:
        return jsonify(data)
    return jsonify({'error': 'Download not found'}), 404

@app.route('/download/<download_id>', methods=['GET'])
def get_file(download_id):
    if not validate_download_id(download_id):
        return jsonify({'error': 'Invalid ID'}), 400
    
    data = get_download(download_id)
    if data and data['status'] == 'completed' and 'filepath' in data:
        if os.path.exists(data['filepath']):
            try:
                return send_file(
                    data['filepath'],
                    as_attachment=True,
                    download_name=sanitize_filename(data.get('filename', 'video.mp4')),
                    max_age=0
                )
            except Exception as e:
                logger.error(f"File send error: {e}")
                return jsonify({'error': 'File corrupted'}), 500
    
    return jsonify({'error': 'File not found'}), 404

@app.route('/cancel/<download_id>', methods=['POST'])
def cancel_download(download_id):
    if not validate_download_id(download_id):
        return jsonify({'error': 'Invalid ID'}), 400
    
    if update_download(download_id, status='cancelled', completed_at=time.time()):
        logger.info(f"Download cancelled: {download_id[:8]}")
        return jsonify({'status': 'cancelled'})
    return jsonify({'error': 'Download not found'}), 404

@app.route('/history', methods=['GET'])
def get_history():
    with history_lock:
        return jsonify(load_history())

@app.route('/test', methods=['GET'])
def test():
    return jsonify({
        'status': 'ok',
        'message': 'YT Downloader API - 25 Pengikut Spesial v3.0',
        'version': '3.0.0',
        'cleanup_running': cleanup_thread.is_alive() if cleanup_thread else False,
        'time': datetime.now().isoformat()
    })

@app.route('/stats', methods=['GET'])
@limiter.limit("5 per minute")
def get_stats():
    """Server statistics"""
    with downloads_lock:
        downloads = {
            'total': len(active_downloads),
            'completed': sum(1 for d in active_downloads.values() if d['status'] == 'completed'),
            'downloading': sum(1 for d in active_downloads.values() if d['status'] in ['starting', 'downloading']),
            'error': sum(1 for d in active_downloads.values() if d['status'] == 'error'),
            'cancelled': sum(1 for d in active_downloads.values() if d['status'] == 'cancelled'),
        }
    
    with cache_lock:
        cache_stats = {
            'size': len(video_cache),
            'max': MAX_CACHE_SIZE,
            'usage': f"{len(video_cache)/MAX_CACHE_SIZE*100:.1f}%" if MAX_CACHE_SIZE > 0 else "0%"
        }
    
    return jsonify({
        'downloads': downloads,
        'cache': cache_stats,
        'uptime': time.time() - app.start_time if hasattr(app, 'start_time') else 0,
        'cleanup_alive': cleanup_thread.is_alive() if cleanup_thread else False
    })

# ============ HEALTH CHECK ============
@app.route('/health', methods=['GET'])
def health():
    """Kubernetes/Render health check"""
    status = 'healthy'
    code = 200
    
    # Check cleanup thread
    if cleanup_thread and not cleanup_thread.is_alive():
        status = 'unhealthy'
        code = 503
    
    return jsonify({
        'status': status,
        'timestamp': datetime.now().isoformat()
    }), code

# ============ ERROR HANDLERS ============
@app.errorhandler(429)
def ratelimit_handler(e):
    logger.warning(f"Rate limit exceeded: {request.remote_addr}")
    return jsonify({
        'error': 'Too many requests - Santai bang!',
        'success': False,
        'retry_after': e.description
    }), 429

@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Endpoint gak ketemu', 'success': False}), 404

@app.errorhandler(500)
def internal_error(e):
    logger.error(f"500 error: {e}")
    return jsonify({'error': 'Internal server error', 'success': False}), 500

# ========================================

if __name__ == '__main__':
    app.start_time = time.time()
    port = int(os.environ.get('PORT', 5000))
    
    logger.info("="*70)
    logger.info("🔥 YOUTUBE DOWNLOADER API - 25 PENGIKUT SPESIAL v3.0")
    logger.info("="*70)
    logger.info("✅ FINAL FIXES:")
    logger.info("   • Stop mechanism cleanup thread: ✅ Graceful shutdown")
    logger.info("   • Atomic history write: ✅ Anti corruption")
    logger.info("   • Logging instead of print: ✅ Production ready")
    logger.info("   • yt-dlp internal retry: ✅ Using native retry")
    logger.info("   • Shorts pattern: ✅ All variations covered")
    logger.info("="*70)
    logger.info(f"📁 Download path: {DOWNLOAD_PATH}")
    logger.info(f"📝 Max history: {MAX_HISTORY_ITEMS} items")
    logger.info(f"🧹 Cleanup interval: {CLEANUP_INTERVAL}s")
    logger.info(f"🔄 Max retries: {MAX_RETRIES} (yt-dlp internal)")
    logger.info(f"🎯 Cache max size: {MAX_CACHE_SIZE}")
    logger.info(f"📋 Log file: /tmp/yt-downloader.log")
    logger.info("="*70)
    logger.info(f"🚀 Server ready on port {port}")
    logger.info("="*70)
    
    try:
        app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        if cleanup_thread:
            cleanup_thread.stop()
            cleanup_thread.join()
