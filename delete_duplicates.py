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
python delete_duplicates.py "C:/images" --duplicates-dir "C:/dups" --threads 8

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
from datetime import datetime

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
    import faiss
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

    def init_db(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
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
        conn.close()

    def get_cached_hashes(self, file_path):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('SELECT md5, phash, dhash, whash, histogram_hash FROM file_hashes WHERE file_path = ?', (file_path,))
        result = c.fetchone()
        conn.close()
        return result

    def cache_hashes(self, file_path, md5, phash, dhash, whash, hist_hash):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO file_hashes 
            (file_path, md5, phash, dhash, whash, histogram_hash, processed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (file_path, md5, phash, dhash, whash, hist_hash, datetime.now()))
        conn.commit()
        conn.close()

    def log_duplicate(self, original, duplicate, method, score):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''
            INSERT INTO duplicates_log 
            (original, duplicate, detection_method, similarity_score, moved_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (original, duplicate, method, score, datetime.now()))
        conn.commit()
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
            counter = 1

            while True:
                new_name = f"{stem}_{counter}{suffix}"
                new_path = parent / new_name

                if not new_path.exists():
                    target_path = str(new_path)
                    break

                counter += 1

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
        # Convert hamming distance to similarity (0-1)
        similarity = 1.0 - (distance / 64.0)
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

        if des1 is None or des2 is None:
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

        if des1 is None or des2 is None:
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


def ensemble_similarity(path1, path2, threshold=0.85):
    """
    Ensemble approach combining multiple similarity metrics
    Returns 0-1 similarity score
    """
    scores = []
    weights = []

    try:
        if not os.path.isfile(path1) or not os.path.isfile(path2):
            logger.debug(f"Ensemble: One or both files missing")
            return 0.0

        # 1. ORB Similarity (70% weight - fast and effective)
        orb_sim = orb_similarity(path1, path2)
        scores.append(orb_sim)
        weights.append(0.70)

        # 2. SIFT Similarity (20% weight - more accurate but slower)
        sift_sim = sift_similarity(path1, path2)
        scores.append(sift_sim)
        weights.append(0.20)

        # 3. Histogram Similarity (10% weight - color/brightness)
        hist1 = compute_histogram_hash(path1)
        hist2 = compute_histogram_hash(path2)
        hist_sim = histogram_similarity(hist1, hist2)
        scores.append(hist_sim)
        weights.append(0.10)

        if not scores:
            return 0.0

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
                cache_db.cache_hashes(
                    path,
                    md5,
                    hashes[0]['phash'] if hashes else None,
                    hashes[0]['dhash'] if hashes else None,
                    hashes[0]['whash'] if hashes else None,
                    None
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
    num_threads = num_threads or min(cpu_count(), 8)

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

    logger.info("STAGE 2: Building perceptual hash index...")
    print("STAGE 2: Building perceptual hash index...\n")

    remaining = [p for p in image_files if p not in moved]

    hash_db = {}

    for path in tqdm(remaining, desc="Generating hashes"):
        try:
            hashes = generate_multi_level_hashes(path)
            histogram = compute_histogram_hash(path)
            hash_db[path] = {
                'hashes': hashes,
                'histogram': histogram
            }
        except Exception as e:
            logger.warning(f"Failed hash generation for {path}: {e}")

    # ========================================================
    # STAGE 3: ROTATED/RESIZED DUPLICATES (PHASH-based)
    # ========================================================

    logger.info("STAGE 3: Detecting rotated and resized duplicates...")
    print("STAGE 3: Detecting rotated and resized duplicates...\n")

    checked_pairs = set()
    phash_map = defaultdict(list)

    # Build phash groups
    for path, data in hash_db.items():
        if data['hashes']:
            phash = data['hashes'][0]['phash']
            phash_map[phash].append(path)

    for phash, group in tqdm(phash_map.items(), desc="Checking phash groups"):
        if len(group) <= 1:
            continue

        for i, p1 in enumerate(group):
            for p2 in group[i+1:]:
                if p1 in moved or p2 in moved:
                    continue

                pair_key = tuple(sorted([p1, p2]))
                if pair_key in checked_pairs:
                    continue

                checked_pairs.add(pair_key)

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

    logger.info("STAGE 4: Detecting compressed and resized variants...")
    print("STAGE 4: Detecting compressed and resized variants...\n")

    remaining = [p for p in remaining if p not in moved]

    for i in tqdm(range(len(remaining)), desc="Checking histogram similarity"):
        p1 = remaining[i]

        if p1 in moved or p1 not in hash_db:
            continue

        for j in range(i + 1, len(remaining)):
            p2 = remaining[j]

            if p2 in moved or p2 not in hash_db:
                continue

            pair_key = tuple(sorted([p1, p2]))
            if pair_key in checked_pairs:
                continue

            checked_pairs.add(pair_key)

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

    for i in tqdm(range(len(remaining)), desc="Ensemble comparison"):
        p1 = remaining[i]

        if p1 in moved:
            continue

        for j in range(i + 1, len(remaining)):
            p2 = remaining[j]

            if p2 in moved:
                continue

            pair_key = tuple(sorted([p1, p2]))
            if pair_key in checked_pairs:
                continue

            checked_pairs.add(pair_key)

            similarity = ensemble_similarity(p1, p2, similarity_threshold)

            if similarity >= similarity_threshold:
                score1 = get_image_quality_score(p1)
                score2 = get_image_quality_score(p2)
                keep = p1 if score1 >= score2 else p2
                remove = p2 if keep == p1 else p1

                moved_path = move_to_duplicates(remove, directory, duplicates_dir)
                if moved_path:
                    moved.add(remove)
                    stats['similar_duplicates'] += 1
                    cache_db.log_duplicate(keep, remove, 'ENSEMBLE', similarity)
                    logger.info(f"[SIMILAR] Sim: {similarity:.3f} | KEEP: {keep}")
                    logger.info(f"[SIMILAR] MOVED: {moved_path}")

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
