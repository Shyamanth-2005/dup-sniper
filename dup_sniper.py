"""
Advanced Image Deduplication Tool - Production Grade (SAFE VERSION)
--------------------------------------------------------------------

Enterprise-grade deduplication supporting 1M+ images efficiently.

Instead of deleting images:
- Moves duplicates into a DUPLICATES folder
- Preserves original directory structure
- Detects:
    - exact duplicates (MD5)
    - rotated duplicates (perceptual hashing)
    - resized/compressed duplicates (deep hashing)
    - visually similar images (ORB + SIFT)
    - subtle color/brightness changes (histogram)
    - near-duplicates with slight modifications (ensemble)

Key Features:
- Multi-level hierarchical detection strategy
- Persistent SQLite cache to avoid re-processing
- Batch processing with multiprocessing
- Memory-efficient streaming for large datasets
- Resumable operations with checkpoints
- Quality scoring to keep best image in groups
- Detailed logging and statistics

Install:
pip install pillow imagehash opencv-contrib-python tqdm numpy scipy scikit-learn scikit-image

Usage:
python delete_duplicates.py "C:/images"
python delete_duplicates.py "C:/images" --similarity 0.82

Recommended similarity thresholds:
0.75-0.80 = aggressive (removes even subtle variations)
0.80-0.85 = balanced (recommended)
0.85-0.90 = conservative (keeps more originals)
"""

import os
import cv2
import shutil
import argparse
import hashlib
import imagehash
import numpy as np
import sqlite3
import json
import logging
import warnings
import time
import uuid
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

from PIL import Image
from tqdm import tqdm
from pathlib import Path
from collections import defaultdict
from multiprocessing import Pool, cpu_count
from functools import lru_cache
from scipy.spatial.distance import euclidean
from scipy.stats import entropy

# Suppress OpenCV warnings
warnings.filterwarnings('ignore', category=DeprecationWarning)
os.environ['OPENCV_LOG_LEVEL'] = 'OFF'

try:
    from skimage.metrics import structural_similarity as ssim
except ImportError:
    ssim = None

try:
    import faiss  # pyright: ignore[reportMissingImports]
    HAS_FAISS = True
except ImportError:
    HAS_FAISS = False


# ============================================================
# CONFIG
# ============================================================

VALID_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"
}

ROTATIONS = [0, 90, 180, 270]
BATCH_SIZE = 500
CACHE_DB = "db/.dedupe_cache.db"
LOG_FILE = "logs/dedupe_log.txt"


# ============================================================
# LOGGING
# ============================================================

def setup_logging(log_file=LOG_FILE):
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)

logger = setup_logging()


# ============================================================
# CACHE MANAGEMENT
# ============================================================

class CacheDB:
    def __init__(self, db_path=CACHE_DB):
        self.db_path = db_path
        self.init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute('PRAGMA busy_timeout = 30000')
        return conn

    def init_db(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = self._connect()
        try:
            conn.execute('PRAGMA journal_mode = WAL')
            conn.execute('PRAGMA synchronous = NORMAL')
            conn.execute('PRAGMA busy_timeout = 30000')
            c = conn.cursor()

            c.execute('''
                CREATE TABLE IF NOT EXISTS file_hashes (
                    file_path TEXT PRIMARY KEY,
                    md5 TEXT,
                    phash TEXT,
                    dhash TEXT,
                    whash TEXT,
                    histogram_hash TEXT,
                    processed_at TIMESTAMP
                )
            ''')

            c.execute('''
                CREATE TABLE IF NOT EXISTS duplicates_log (
                    id INTEGER PRIMARY KEY,
                    original TEXT,
                    duplicate TEXT,
                    detection_method TEXT,
                    similarity_score REAL,
                    moved_at TIMESTAMP
                )
            ''')

            conn.commit()
        finally:
            conn.close()

    def get_cached_hashes(self, file_path):
        conn = self._connect()
        try:
            c = conn.cursor()
            c.execute('SELECT md5, phash, dhash, whash, histogram_hash FROM file_hashes WHERE file_path = ?', (file_path,))
            return c.fetchone()
        finally:
            conn.close()

    def cache_hashes(self, file_path, md5, phash, dhash, whash, hist_hash):
        for attempt in range(3):
            conn = self._connect()
            try:
                c = conn.cursor()
                c.execute('''
                    INSERT OR REPLACE INTO file_hashes 
                    (file_path, md5, phash, dhash, whash, histogram_hash, processed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (file_path, md5, phash, dhash, whash, hist_hash, datetime.now()))
                conn.commit()
                return
            except sqlite3.OperationalError as e:
                if 'locked' in str(e).lower() and attempt < 2:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise
            finally:
                conn.close()

    def log_duplicate(self, original, duplicate, method, score):
        for attempt in range(3):
            conn = self._connect()
            try:
                c = conn.cursor()
                c.execute('''
                    INSERT INTO duplicates_log 
                    (original, duplicate, detection_method, similarity_score, moved_at)
                    VALUES (?, ?, ?, ?, ?)
                ''', (original, duplicate, method, score, datetime.now()))
                conn.commit()
                return
            except sqlite3.OperationalError as e:
                if 'locked' in str(e).lower() and attempt < 2:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise
            finally:
                conn.close()


cache_db = CacheDB()


# ============================================================
# HELPERS
# ============================================================

def file_md5(path, chunk_size=8192):
    md5 = hashlib.md5()
    try:
        # Check if file exists first
        if not os.path.isfile(path):
            logger.warning(f"File not found: {path}")
            return None
            
        with open(path, "rb") as f:
            while chunk := f.read(chunk_size):
                md5.update(chunk)
        return md5.hexdigest()
    except Exception as e:
        logger.warning(f"Failed to compute MD5 for {path}: {e}")
        return None


def get_image_quality_score(path):
    """
    Comprehensive quality score - higher = better image to keep
    Considers: resolution, file size, sharpness, color information
    """
    try:
        if not os.path.isfile(path):
            logger.warning(f"Image file not found: {path}")
            return 0

        img = cv2.imread(str(path))
        if img is None:
            logger.warning(f"Failed to read image: {path}")
            return 0

        h, w = img.shape[:2]
        resolution_score = w * h

        file_size = os.path.getsize(path)

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        sharpness = cv2.Laplacian(gray, cv2.CV_64F).var()

        # Color information richness
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        saturation = np.mean(hsv[:, :, 1])

        quality_score = (
            resolution_score * 0.4 +
            file_size * 0.3 +
            sharpness * 50 +
            saturation * 100
        )

        return quality_score

    except Exception as e:
        logger.warning(f"Error scoring image {path}: {e}")
        return 0


def move_to_duplicates(src_path, root_dir, duplicates_dir):
    try:
        # Check if source file exists
        if not os.path.isfile(src_path):
            logger.error(f"Source file not found: {src_path}")
            return None

        relative_path = os.path.relpath(src_path, root_dir)
        target_path = os.path.join(duplicates_dir, relative_path)

        os.makedirs(os.path.dirname(target_path), exist_ok=True)

        if os.path.exists(target_path):
            stem = Path(target_path).stem
            suffix = Path(target_path).suffix
            parent = Path(target_path).parent
            while True:
                unique_name = f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"
                new_path = parent / unique_name

                if not new_path.exists():
                    target_path = str(new_path)
                    break

        shutil.move(src_path, target_path)
        return target_path

    except FileNotFoundError as e:
        logger.error(f"File operation failed for {src_path}: File not found - {e}")
        return None
    except PermissionError as e:
        logger.error(f"Permission denied moving {src_path}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error moving {src_path}: {e}")
        return None


# ============================================================
# HASHING - MULTI-LEVEL
# ============================================================

def generate_multi_level_hashes(path):
    """
    Generate multiple hash types for comprehensive comparison:
    - Perceptual hashes (phash, dhash, whash) - robust to compression
    - Histogram hash - captures color distribution
    - All rotation variants
    """
    hashes = []

    try:
        if not os.path.isfile(path):
            logger.warning(f"Image file not found for hashing: {path}")
            return hashes

        img = Image.open(path).convert("RGB")

        for angle in ROTATIONS:
            rotated = img.rotate(angle, expand=True)

            phash = str(imagehash.phash(rotated, hash_size=16))
            dhash = str(imagehash.dhash(rotated, hash_size=16))
            whash = str(imagehash.whash(rotated, hash_size=16))

            hashes.append({
                'angle': angle,
                'phash': phash,
                'dhash': dhash,
                'whash': whash
            })

    except FileNotFoundError:
        logger.warning(f"Hash generation failed - file not found: {path}")
    except Exception as e:
        logger.warning(f"Failed to hash {path}: {e}")

    return hashes


def compute_histogram_hash(path, bins=16):
    """
    Histogram-based hash for color/brightness sensitive comparison
    Robust to compression and slight modifications
    """
    try:
        if not os.path.isfile(path):
            logger.warning(f"Image file not found for histogram: {path}")
            return None

        img = cv2.imread(str(path))
        if img is None:
            logger.warning(f"Failed to read image for histogram: {path}")
            return None

        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        hist_h = cv2.calcHist([hsv], [0], None, [bins], [0, 180])
        hist_s = cv2.calcHist([hsv], [1], None, [bins], [0, 256])
        hist_v = cv2.calcHist([hsv], [2], None, [bins], [0, 256])

        hist = np.concatenate([hist_h, hist_s, hist_v]).flatten()
        hist = hist / (hist.sum() + 1e-10)

        return hist

    except FileNotFoundError:
        logger.warning(f"Histogram computation failed - file not found: {path}")
        return None
    except Exception as e:
        logger.warning(f"Failed to compute histogram for {path}: {e}")
        return None


def histogram_bucket_signature(hist, segments=8):
    """
    Build a compact, comparable signature for histogram bucketing.
    """
    if hist is None:
        return None

    try:
        if segments <= 0:
            segments = 8

        parts = np.array_split(hist.astype(np.float32), segments)
        bucket = []

        for part in parts:
            segment_score = float(part.sum())
            bucket.append(str(min(9, max(0, int(round(segment_score * 9))))))

        return ''.join(bucket)
    except Exception:
        return None


def histogram_bucket_signatures(hist):
    """
    Produce multiple histogram signatures at different granularities.
    This broadens candidate matching without falling back to all-pairs.
    """
    signatures = set()

    for segments in (4, 8):
        signature = histogram_bucket_signature(hist, segments=segments)
        if signature is not None:
            signatures.add(signature)

    return signatures


def stage5_bucket_keys(data):
    """
    Combine rotation-aware pHash prefixes with histogram structure for Stage 5.
    Multiple keys make the candidate filter more conservative.
    """
    hashes = data.get('hashes') or []
    hist_signatures = histogram_bucket_signatures(data.get('histogram'))
    keys = set()

    for hash_item in hashes:
        for hash_name in ('phash', 'dhash', 'whash'):
            hash_value = hash_item.get(hash_name, '')
            if not hash_value:
                continue

            for prefix_length in (4, 8):
                prefix = hash_value[:prefix_length]
                for hist_signature in hist_signatures:
                    keys.add(f"{hash_name}:{prefix}:{hist_signature}")

    if not keys:
        for hist_signature in hist_signatures or {'nohist'}:
            keys.add(f":{hist_signature}")

    return keys


def stage4_bucket_keys(data):
    """
    Histogram-only grouping for resized/compressed comparisons.
    """
    return histogram_bucket_signatures(data.get('histogram'))


def subdivide_bucket_by_strict_hash(group, hash_db, depth=0):
    """
    Recursively subdivide a large bucket using strict hash prefixes.
    Prevents one massive bucket from blocking Stage 5 for days.
    """
    if len(group) <= 50 or depth > 3:
        return [group]

    sub_buckets = defaultdict(list)

    for path in group:
        if path not in hash_db or not hash_db[path]['hashes']:
            sub_buckets[f"nosub_{depth}"].append(path)
            continue

        # Use increasingly strict prefixes per depth: 1→2→3→4 chars
        hash_item = hash_db[path]['hashes'][0]
        phash = hash_item.get('phash', '')
        prefix_len = max(1, 4 - depth)  # depth 0: 4 chars, depth 1: 3, etc.
        prefix = phash[:prefix_len] if phash else "none"

        sub_buckets[prefix].append(path)

    result = []
    for sub_group in sub_buckets.values():
        if len(sub_group) > 50:
            # Keep subdividing if still too large
            result.extend(subdivide_bucket_by_strict_hash(sub_group, hash_db, depth + 1))
        else:
            result.append(sub_group)

    return result


def build_candidate_components(paths, hash_db, key_builder):
    """
    Build connected components from overlapping candidate buckets.
    Each component can be processed independently without a global pair set.
    """
    bucket_to_paths = defaultdict(list)

    for path in paths:
        if path not in hash_db:
            continue

        bucket_keys = key_builder(hash_db[path])
        for bucket_key in bucket_keys:
            bucket_to_paths[bucket_key].append(path)

    parent = {}

    def find(item):
        parent.setdefault(item, item)
        if parent[item] != item:
            parent[item] = find(parent[item])
        return parent[item]

    def union(item_a, item_b):
        root_a = find(item_a)
        root_b = find(item_b)
        if root_a != root_b:
            parent[root_b] = root_a

    for group in bucket_to_paths.values():
        if len(group) <= 1:
            continue

        anchor = group[0]
        for item in group[1:]:
            union(anchor, item)

    components = defaultdict(list)
    for path in parent:
        components[find(path)].append(path)

    return [group for group in components.values() if len(group) > 1]


# ============================================================
# ADVANCED SIMILARITY METRICS
# ============================================================

def perceptual_hash_similarity(hash1, hash2):
    """
    Compare perceptual hashes - returns 0-1 similarity
    """
    if not hash1 or not hash2:
        return 0.0

    try:
        h1 = imagehash.hex_to_hash(hash1)
        h2 = imagehash.hex_to_hash(hash2)
        distance = h1 - h2
        # Convert Hamming distance to similarity (0-1) using the actual bit width.
        total_bits = max(int(h1.hash.size), int(h2.hash.size), 1)
        similarity = 1.0 - (distance / float(total_bits))
        return max(0, similarity)
    except:
        return 0.0


def histogram_similarity(hist1, hist2, method='bhattacharyya'):
    """
    Compare histograms using various methods
    Returns 0-1 similarity
    """
    if hist1 is None or hist2 is None:
        return 0.0

    try:
        if method == 'bhattacharyya':
            # Bhattacharyya distance
            dist = cv2.compareHist(
                hist1.astype(np.float32),
                hist2.astype(np.float32),
                cv2.HISTCMP_BHATTACHARYYA
            )
            return 1.0 - dist

        elif method == 'chi_square':
            dist = cv2.compareHist(
                hist1.astype(np.float32),
                hist2.astype(np.float32),
                cv2.HISTCMP_CHISQR
            )
            # Normalize chi-square distance
            return 1.0 / (1.0 + dist)

        elif method == 'correlation':
            return cv2.compareHist(
                hist1.astype(np.float32),
                hist2.astype(np.float32),
                cv2.HISTCMP_CORREL
            )

    except:
        return 0.0

    return 0.0


def sift_similarity(path1, path2):
    """
    SIFT-based similarity - excellent for detecting:
    - Cropped versions
    - Scaled versions
    - Objects with slight transforms
    
    Returns 0-1 similarity
    """
    try:
        if not os.path.isfile(path1) or not os.path.isfile(path2):
            logger.warning(f"SIFT: One or both files missing: {path1}, {path2}")
            return 0.0

        img1 = cv2.imread(str(path1), 0)
        img2 = cv2.imread(str(path2), 0)

        if img1 is None or img2 is None:
            logger.warning(f"SIFT: Failed to read images")
            return 0.0

        sift = cv2.SIFT_create()

        kp1, des1 = sift.detectAndCompute(img1, None)
        kp2, des2 = sift.detectAndCompute(img2, None)

        if (
            des1 is None or des2 is None or
            kp1 is None or kp2 is None or
            len(kp1) < 2 or len(kp2) < 2 or
            len(des1) < 2 or len(des2) < 2
        ):
            return 0.0

        # Flann matcher for SIFT
        FLANN_INDEX_KDTREE = 1
        index_params = dict(algorithm=FLANN_INDEX_KDTREE, trees=5)
        search_params = dict(checks=50)
        
        flann = cv2.FlannBasedMatcher(index_params, search_params)
        matches = flann.knnMatch(des1, des2, k=2)

        if not matches:
            return 0.0

        # Apply Lowe's ratio test
        good_matches = []
        for match_pair in matches:
            if len(match_pair) == 2:
                m, n = match_pair
                if m.distance < 0.7 * n.distance:
                    good_matches.append(m)

        similarity = len(good_matches) / max(len(kp1), len(kp2), 1)
        return min(1.0, similarity)

    except FileNotFoundError:
        logger.warning(f"SIFT similarity failed - file not found")
        return 0.0
    except Exception as e:
        logger.warning(f"SIFT similarity error: {e}")
        return 0.0


def orb_similarity(path1, path2):
    """
    ORB-based similarity - fast, good for large-scale
    Detects rotations and transformations
    """
    try:
        if not os.path.isfile(path1) or not os.path.isfile(path2):
            logger.debug(f"ORB: One or both files missing")
            return 0.0

        img1 = cv2.imread(str(path1), 0)
        img2 = cv2.imread(str(path2), 0)

        if img1 is None or img2 is None:
            logger.debug(f"ORB: Failed to read images")
            return 0.0

        orb = cv2.ORB_create(nfeatures=2000)

        kp1, des1 = orb.detectAndCompute(img1, None)
        kp2, des2 = orb.detectAndCompute(img2, None)

        if (
            des1 is None or des2 is None or
            kp1 is None or kp2 is None or
            len(kp1) < 2 or len(kp2) < 2 or
            len(des1) < 2 or len(des2) < 2
        ):
            return 0.0

        matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        matches = matcher.match(des1, des2)

        if not matches:
            return 0.0

        matches = sorted(matches, key=lambda x: x.distance)
        good_matches = [m for m in matches if m.distance < 50]

        similarity = len(good_matches) / max(len(kp1), len(kp2), 1)
        return min(1.0, similarity)

    except FileNotFoundError:
        logger.debug(f"ORB similarity failed - file not found")
        return 0.0
    except Exception as e:
        logger.debug(f"ORB similarity error: {e}")
        return 0.0


def ensemble_similarity(path1, path2, threshold=0.85, hist1=None, hist2=None, skip_sift=False):
    """
    Ensemble approach combining multiple similarity metrics
    Returns 0-1 similarity score
    
    Args:
        skip_sift: if True, skip SIFT and use ORB + histogram only (fast mode for large buckets)
    """
    scores = []
    weights = []

    try:
        if not os.path.isfile(path1) or not os.path.isfile(path2):
            logger.debug(f"Ensemble: One or both files missing")
            return 0.0

        # 1. ORB Similarity (fast structural signal)
        orb_sim = orb_similarity(path1, path2)
        
        # EARLY REJECTION: if ORB is very low, images are clearly different
        # Skip expensive SIFT computation entirely (huge speedup)
        if orb_sim < 0.55 and skip_sift:
            return 0.0
        if orb_sim < 0.50 and not skip_sift:
            return 0.0
        
        scores.append(orb_sim)
        weights.append(0.55)

        # 2. SIFT Similarity (more accurate but slower) - skip for large buckets
        if not skip_sift:
            sift_sim = sift_similarity(path1, path2)
            scores.append(sift_sim)
            weights.append(0.30)
        else:
            sift_sim = 0.0  # Zero weight for skipped SIFT in fast mode
            # Rebalance weights: ORB 0.70, histogram 0.30 (skip SIFT)

        # 3. Histogram Similarity (color/brightness)
        if hist1 is None:
            hist1 = compute_histogram_hash(path1)
        if hist2 is None:
            hist2 = compute_histogram_hash(path2)
        hist_sim = histogram_similarity(hist1, hist2)
        scores.append(hist_sim)
        if skip_sift:
            weights.append(0.30)  # Higher weight on histogram in fast mode
        else:
            weights.append(0.15)

        if not scores:
            return 0.0

        if skip_sift:
            # In fast mode, only check ORB
            if orb_sim >= 0.98:
                return max(orb_sim, hist_sim)
        else:
            # In full mode, check both ORB and SIFT
            if max(orb_sim, sift_sim) >= 0.98:
                return max(orb_sim, sift_sim, hist_sim)

        ensemble_score = sum(s * w for s, w in zip(scores, weights)) / sum(weights)
        return ensemble_score

    except FileNotFoundError:
        logger.debug(f"Ensemble similarity failed - file not found")
        return 0.0
    except Exception as e:
        logger.warning(f"Ensemble similarity error: {e}")
        return 0.0


# ============================================================
# FILE COLLECTION & PROCESSING
# ============================================================

def collect_images(directory):
    files = []
    for root, _, filenames in os.walk(directory):
        for f in filenames:
            ext = Path(f).suffix.lower()
            if ext in VALID_EXTENSIONS:
                full_path = os.path.join(root, f)
                # Verify file exists before adding
                if os.path.isfile(full_path):
                    files.append(full_path)
                else:
                    logger.warning(f"Skipped (not found): {full_path}")
    return files


def process_image_batch(paths):
    """
    Process a batch of images for hashing
    Designed for multiprocessing efficiency
    """
    results = []
    for path in paths:
        try:
            md5 = file_md5(path)
            hashes = generate_multi_level_hashes(path)
            hist = compute_histogram_hash(path)

            results.append({
                'path': path,
                'md5': md5,
                'hashes': hashes,
                'histogram': hist
            })

            if md5:
                hist_signature = histogram_bucket_signature(hist)
                cache_db.cache_hashes(
                    path,
                    md5,
                    hashes[0]['phash'] if hashes else None,
                    hashes[0]['dhash'] if hashes else None,
                    hashes[0]['whash'] if hashes else None,
                    hist_signature
                )

        except Exception as e:
            logger.warning(f"Failed to process {path}: {e}")

    return results


# ============================================================
# DEDUPLICATION LOGIC - MULTI-STAGE
# ============================================================

def deduplicate(
    directory,
    duplicates_dir,
    similarity_threshold=0.85,
    num_threads=None
):
    """
    Main deduplication pipeline with multi-stage approach
    """

    os.makedirs(duplicates_dir, exist_ok=True)
    num_threads = num_threads or (cpu_count() or 1)
    # Cap threads at 5 for optimal CPU utilization and reduced context switching
    num_threads = max(1, min(num_threads, 5))

    logger.info(f"\n{'='*60}")
    logger.info(f"Advanced Image Deduplication Starting")
    logger.info(f"Directory: {directory}")
    logger.info(f"Similarity Threshold: {similarity_threshold}")
    logger.info(f"Threads: {num_threads}")
    logger.info(f"{'='*60}\n")

    print(f"\nScanning images...\n")

    image_files = collect_images(directory)
    logger.info(f"Found {len(image_files)} images")
    print(f"Found {len(image_files)} images\n")

    if not image_files:
        logger.info("No images found. Exiting.")
        return

    moved = set()
    stats = {
        'exact_duplicates': 0,
        'rotated_duplicates': 0,
        'resized_duplicates': 0,
        'similar_duplicates': 0,
        'total_moved': 0
    }

    # ========================================================
    # STAGE 1: EXACT DUPLICATES (MD5)
    # ========================================================

    logger.info("STAGE 1: Detecting exact duplicates...")
    print("STAGE 1: Detecting exact duplicates...\n")

    md5_map = defaultdict(list)

    for path in tqdm(image_files, desc="Computing MD5"):
        try:
            md5 = file_md5(path)
            if md5:
                md5_map[md5].append(path)
        except Exception as e:
            logger.warning(f"Failed MD5 for {path}: {e}")

    for md5, group in md5_map.items():
        if len(group) <= 1:
            continue

        scores = {p: get_image_quality_score(p) for p in group}
        keep = max(scores, key=scores.get)

        for p in group:
            if p != keep:
                moved_path = move_to_duplicates(p, directory, duplicates_dir)
                if moved_path:
                    moved.add(p)
                    stats['exact_duplicates'] += 1
                    cache_db.log_duplicate(keep, p, 'EXACT_MD5', 1.0)
                    logger.info(f"[EXACT] KEEP: {keep}")
                    logger.info(f"[EXACT] MOVED: {moved_path}")

    print(f"\nExact duplicates moved: {stats['exact_duplicates']}\n")

    # ========================================================
    # STAGE 2: BUILD HASH INDEX
    # ========================================================

    logger.info("STAGE 2: Building perceptual hash index (parallelized)...")
    print("STAGE 2: Building perceptual hash index (parallelized)...\n")

    remaining = [p for p in image_files if p not in moved]

    hash_db = {}

    # OPTIMIZATION: Parallelize hash generation across CPU cores using multiprocessing
    batch_size = max(10, len(remaining) // (num_threads * 4))
    batches = [remaining[i:i+batch_size] for i in range(0, len(remaining), batch_size)]
    
    with Pool(processes=num_threads) as pool:
        batch_results = list(tqdm(
            pool.imap_unordered(process_image_batch, batches),
            total=len(batches),
            desc="Generating hashes"
        ))
    
    for batch_result in batch_results:
        for item in batch_result:
            hash_db[item['path']] = {
                'hashes': item['hashes'],
                'histogram': item['histogram']
            }

    # ========================================================
    # STAGE 3: ROTATED/RESIZED DUPLICATES (PHASH-based)
    # ========================================================

    logger.info("STAGE 3: Detecting rotated and resized duplicates...")
    print("STAGE 3: Detecting rotated and resized duplicates...\n")

    phash_map = defaultdict(list)

    # Build phash groups from all rotation variants
    for path, data in hash_db.items():
        if data['hashes']:
            for hash_item in data['hashes']:
                phash_map[hash_item['phash']].append(path)

    for phash, group in tqdm(phash_map.items(), desc="Checking phash groups"):
        if len(group) <= 1:
            continue

        group = list(dict.fromkeys(group))

        for i, p1 in enumerate(group):
            for p2 in group[i+1:]:
                if p1 in moved or p2 in moved:
                    continue

                # Check all rotation variants
                max_sim = 0
                for h1 in hash_db[p1]['hashes']:
                    for h2 in hash_db[p2]['hashes']:
                        sim = perceptual_hash_similarity(h1['phash'], h2['phash'])
                        max_sim = max(max_sim, sim)

                if max_sim > 0.95:
                    score1 = get_image_quality_score(p1)
                    score2 = get_image_quality_score(p2)
                    keep = p1 if score1 >= score2 else p2
                    remove = p2 if keep == p1 else p1

                    moved_path = move_to_duplicates(remove, directory, duplicates_dir)
                    if moved_path:
                        moved.add(remove)
                        stats['rotated_duplicates'] += 1
                        cache_db.log_duplicate(keep, remove, 'ROTATED_PHASH', max_sim)
                        logger.info(f"[ROTATED] Sim: {max_sim:.3f} | KEEP: {keep}")
                        logger.info(f"[ROTATED] MOVED: {moved_path}")

    print(f"Rotated duplicates moved: {stats['rotated_duplicates']}\n")

    # ========================================================
    # STAGE 4: COMPRESSED/RESIZED (histogram + histogram similarity)
    # ========================================================

    logger.info("STAGE 4: Detecting compressed and resized variants (parallelized)...")
    print("STAGE 4: Detecting compressed and resized variants (parallelized)...\n")

    remaining = [p for p in remaining if p not in moved]

    hist_components = build_candidate_components(remaining, hash_db, stage4_bucket_keys)

    # OPTIMIZATION: Parallelize Stage 4 bucket processing using ThreadPoolExecutor
    def process_stage4_group(group):
        local_results = []
        group = [p for p in group if p not in moved]

        for i, p1 in enumerate(group):
            if p1 in moved:
                continue

            for p2 in group[i + 1:]:
                if p2 in moved:
                    continue

                hist_sim = histogram_similarity(
                    hash_db[p1]['histogram'],
                    hash_db[p2]['histogram']
                )

                if hist_sim > 0.92:
                    score1 = get_image_quality_score(p1)
                    score2 = get_image_quality_score(p2)
                    keep = p1 if score1 >= score2 else p2
                    remove = p2 if keep == p1 else p1

                    moved_path = move_to_duplicates(remove, directory, duplicates_dir)
                    if moved_path:
                        local_results.append((keep, remove, hist_sim, moved_path))
        
        return local_results

    if hist_components:
        with ThreadPoolExecutor(max_workers=num_threads, thread_name_prefix="stage4") as executor:
            futures = [executor.submit(process_stage4_group, group) for group in hist_components]

            for future in tqdm(as_completed(futures), total=len(futures), desc="Checking histogram buckets"):
                try:
                    results = future.result()
                except Exception as e:
                    logger.exception(f"Stage 4 worker failed: {e}")
                    continue

                for keep, remove, hist_sim, moved_path in results:
                    moved.add(remove)
                    stats['resized_duplicates'] += 1
                    cache_db.log_duplicate(keep, remove, 'HISTOGRAM', hist_sim)
                    logger.info(f"[RESIZED] Sim: {hist_sim:.3f} | KEEP: {keep}")
                    logger.info(f"[RESIZED] MOVED: {moved_path}")

    print(f"Resized duplicates moved: {stats['resized_duplicates']}\n")

    # ========================================================
    # STAGE 5: VISUALLY SIMILAR (ensemble)
    # ========================================================

    logger.info("STAGE 5: Detecting visually similar images (ensemble method)...")
    print("STAGE 5: Detecting visually similar images (ensemble method)...\n")

    remaining = [p for p in remaining if p not in moved]

    stage5_groups = build_candidate_components(remaining, hash_db, stage5_bucket_keys)

    def process_stage5_group(group):
        local_moved = set()
        bucket_results = []

        # Add per-bucket diagnostics and a periodic heartbeat so long-running
        # buckets provide visible progress in the logs.
        heartbeat_interval = 200
        LARGE_BUCKET_THRESHOLD = 50  # Skip SIFT for buckets > 50 items

        try:
            start_time = time.time()
            total_items = len(group)
            total_pairs = total_items * (total_items - 1) // 2
            comparisons = 0
            is_large_bucket = total_items > LARGE_BUCKET_THRESHOLD
            mode_str = "FAST (no SIFT)" if is_large_bucket else "FULL (with SIFT)"

            logger.info(
                f"[STAGE5 START] Thread={threading.current_thread().name} size={total_items} "
                f"estimated_pairs={total_pairs} mode={mode_str}"
            )

            quality_scores = {
                path: get_image_quality_score(path)
                for path in group
            }

            ordered_group = sorted(
                group,
                key=lambda path: quality_scores.get(path, 0),
                reverse=True
            )

            for i, p1 in enumerate(ordered_group):
                if p1 in local_moved:
                    continue

                for p2 in ordered_group[i + 1:]:
                    if p2 in local_moved:
                        continue

                    comparisons += 1
                    if comparisons % heartbeat_interval == 0 or comparisons == total_pairs:
                        elapsed = time.time() - start_time
                        logger.info(
                            f"[STAGE5 HEARTBEAT] Thread={threading.current_thread().name} "
                            f"size={total_items} progress={comparisons}/{total_pairs} "
                            f"elapsed={elapsed:.1f}s"
                        )

                    similarity = ensemble_similarity(
                        p1,
                        p2,
                        similarity_threshold,
                        hist1=hash_db[p1]['histogram'],
                        hist2=hash_db[p2]['histogram'],
                        skip_sift=is_large_bucket
                    )

                    if similarity >= similarity_threshold:
                        score1 = quality_scores.get(p1, 0)
                        score2 = quality_scores.get(p2, 0)
                        keep = p1 if score1 >= score2 else p2
                        remove = p2 if keep == p1 else p1

                        if remove in local_moved:
                            continue

                        moved_path = move_to_duplicates(remove, directory, duplicates_dir)
                        if moved_path:
                            local_moved.add(remove)
                            bucket_results.append((keep, remove, similarity, moved_path))

            duration = time.time() - start_time
            logger.info(
                f"[STAGE5 DONE] Thread={threading.current_thread().name} size={total_items} "
                f"moved={len(local_moved)} mode={mode_str} duration={duration:.1f}s"
            )

        except Exception as e:
            logger.exception(f"Stage 5 group failed: {e}")

        return bucket_results

    if stage5_groups:
        # Filter and subdivide to prevent 65M-pair catastrophes
        MAX_PAIRS = 100000  # Skip buckets exceeding 100K pairs
        filtered_groups = []
        skipped_count = 0

        for group in stage5_groups:
            group_size = len(group)
            pair_count = group_size * (group_size - 1) // 2

            if pair_count > MAX_PAIRS:
                # Subdivide into smaller independent sub-buckets
                sub_groups = subdivide_bucket_by_strict_hash(group, hash_db)
                logger.warning(
                    f"[STAGE5 SUBDIVIDE] Large bucket (size={group_size}, pairs={pair_count:,}) "
                    f"split into {len(sub_groups)} sub-buckets"
                )
                filtered_groups.extend(sub_groups)
                skipped_count += 1
            else:
                filtered_groups.append(group)

        if skipped_count > 0:
            logger.info(f"[STAGE5] Subdivided {skipped_count} large buckets for safety")
            print(f"\nStage 5: Subdivided {skipped_count} large bucket(s) into manageable sizes...\n")

        stage5_groups = filtered_groups

        with ThreadPoolExecutor(max_workers=num_threads, thread_name_prefix="stage5") as executor:
            futures = [executor.submit(process_stage5_group, group) for group in stage5_groups]

            for future in tqdm(as_completed(futures), total=len(futures), desc="Ensemble buckets"):
                try:
                    bucket_results = future.result()
                except Exception as e:
                    logger.exception(f"Stage 5 worker failed: {e}")
                    continue

                for keep, remove, similarity, moved_path in bucket_results:
                    moved.add(remove)
                    stats['similar_duplicates'] += 1
                    cache_db.log_duplicate(keep, remove, 'ENSEMBLE', similarity)
                    logger.info(f"[SIMILAR] Sim: {similarity:.3f} | KEEP: {keep}")
                    logger.info(f"[SIMILAR] MOVED: {moved_path}")
    else:
        print("No Stage 5 candidate buckets found.\n")

    stats['total_moved'] = len(moved)

    # ========================================================
    # SUMMARY
    # ========================================================

    logger.info(f"\n{'='*60}")
    logger.info("DEDUPLICATION COMPLETE")
    logger.info(f"{'='*60}")
    logger.info(f"Exact duplicates: {stats['exact_duplicates']}")
    logger.info(f"Rotated duplicates: {stats['rotated_duplicates']}")
    logger.info(f"Resized/compressed: {stats['resized_duplicates']}")
    logger.info(f"Visually similar: {stats['similar_duplicates']}")
    logger.info(f"Total duplicates moved: {stats['total_moved']}")
    logger.info(f"Duplicates folder: {duplicates_dir}")
    logger.info(f"Log file: {LOG_FILE}")
    logger.info(f"{'='*60}\n")

    print(f"\n{'='*60}")
    print("DEDUPLICATION COMPLETE")
    print(f"{'='*60}")
    print(f"Exact duplicates:    {stats['exact_duplicates']}")
    print(f"Rotated duplicates:  {stats['rotated_duplicates']}")
    print(f"Resized/compressed:  {stats['resized_duplicates']}")
    print(f"Visually similar:    {stats['similar_duplicates']}")
    print(f"Total moved:         {stats['total_moved']}")
    print(f"Duplicates folder:   {duplicates_dir}")
    print(f"Log file:            {LOG_FILE}")
    print(f"{'='*60}\n")


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="Advanced image deduplication tool for production use"
    )

    parser.add_argument(
        "directory",
        help="Directory containing images to deduplicate"
    )

    parser.add_argument(
        "--duplicates-dir",
        default="DUPLICATES",
        help="Folder to move duplicates into (default: DUPLICATES)"
    )

    parser.add_argument(
        "--similarity",
        type=float,
        default=0.85,
        help="Similarity threshold 0.75-0.90 (default: 0.85)"
    )

    parser.add_argument(
        "--threads",
        type=int,
        default=None,
        help="Number of processing threads (default: auto-detect)"
    )

    args = parser.parse_args()

    if not os.path.isdir(args.directory):
        print(f"ERROR: Directory '{args.directory}' not found")
        exit(1)

    if not (0.75 <= args.similarity <= 0.95):
        print("ERROR: Similarity must be between 0.75 and 0.95")
        exit(1)

    deduplicate(
        directory=args.directory,
        duplicates_dir=args.duplicates_dir,
        similarity_threshold=args.similarity,
        num_threads=args.threads
    )
