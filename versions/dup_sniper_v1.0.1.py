"""
Advanced Image Deduplication Tool (SAFE VERSION)
------------------------------------------------

Instead of deleting images:
- Moves duplicates into a DUPLICATES folder
- Preserves original directory structure
- Detects:
    - exact duplicates
    - rotated duplicates
    - resized/compressed duplicates
    - visually similar images
    - slight color/brightness changes

Install:
pip install pillow imagehash opencv-python tqdm numpy

Usage:
python dedupe_safe.py "C:/images"

Optional:
python dedupe_safe.py "C:/images" --similarity 0.82
python dedupe_safe.py "C:/images" --duplicates-dir "C:/dups"

Recommended similarity:
0.80 = aggressive
0.88 = safer
"""

import os
import cv2
import shutil
import argparse
import hashlib
import imagehash
import numpy as np

from PIL import Image
from tqdm import tqdm
from pathlib import Path
from collections import defaultdict


# ============================================================
# CONFIG
# ============================================================

VALID_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".tiff"
}

ROTATIONS = [0, 90, 180, 270]


# ============================================================
# HELPERS
# ============================================================

def file_md5(path, chunk_size=8192):
    md5 = hashlib.md5()

    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            md5.update(chunk)

    return md5.hexdigest()


def get_image_quality_score(path):
    """
    Higher score = better image to keep
    """

    try:
        img = cv2.imread(str(path))

        if img is None:
            return 0

        h, w = img.shape[:2]

        resolution_score = w * h

        file_size = os.path.getsize(path)

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        sharpness = cv2.Laplacian(gray, cv2.CV_64F).var()

        return resolution_score + file_size + sharpness

    except:
        return 0


def move_to_duplicates(src_path, root_dir, duplicates_dir):

    try:
        relative_path = os.path.relpath(src_path, root_dir)

        target_path = os.path.join(
            duplicates_dir,
            relative_path
        )

        os.makedirs(os.path.dirname(target_path), exist_ok=True)

        # Prevent overwrite
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

    except Exception as e:
        print(f"ERROR moving {src_path}")
        print(e)
        return None


# ============================================================
# HASHING
# ============================================================

def generate_hashes(path):

    hashes = []

    try:
        img = Image.open(path).convert("RGB")

        for angle in ROTATIONS:

            rotated = img.rotate(angle, expand=True)

            phash = imagehash.phash(rotated)
            dhash = imagehash.dhash(rotated)
            whash = imagehash.whash(rotated)

            hashes.append(
                (
                    str(phash),
                    str(dhash),
                    str(whash)
                )
            )

    except:
        pass

    return hashes


# ============================================================
# ORB FEATURE MATCHING
# ============================================================

def orb_similarity(path1, path2):

    try:
        img1 = cv2.imread(str(path1), 0)
        img2 = cv2.imread(str(path2), 0)

        if img1 is None or img2 is None:
            return 0.0

        orb = cv2.ORB_create(2000)

        kp1, des1 = orb.detectAndCompute(img1, None)
        kp2, des2 = orb.detectAndCompute(img2, None)

        if des1 is None or des2 is None:
            return 0.0

        matcher = cv2.BFMatcher(
            cv2.NORM_HAMMING,
            crossCheck=True
        )

        matches = matcher.match(des1, des2)

        if not matches:
            return 0.0

        matches = sorted(matches, key=lambda x: x.distance)

        good_matches = [
            m for m in matches
            if m.distance < 50
        ]

        similarity = len(good_matches) / max(len(kp1), len(kp2))

        return similarity

    except:
        return 0.0


# ============================================================
# FILE COLLECTION
# ============================================================

def collect_images(directory):

    files = []

    for root, _, filenames in os.walk(directory):

        for f in filenames:

            ext = Path(f).suffix.lower()

            if ext in VALID_EXTENSIONS:
                files.append(os.path.join(root, f))

    return files


# ============================================================
# MAIN
# ============================================================

def deduplicate(
    directory,
    duplicates_dir,
    similarity_threshold=0.80
):

    os.makedirs(duplicates_dir, exist_ok=True)

    print("\nScanning images...\n")

    image_files = collect_images(directory)

    print(f"Found {len(image_files)} images\n")

    moved = set()

    # ========================================================
    # STEP 1 — EXACT DUPLICATES
    # ========================================================

    print("STEP 1: Exact duplicate detection...\n")

    md5_map = defaultdict(list)

    for path in tqdm(image_files):

        try:
            md5 = file_md5(path)
            md5_map[md5].append(path)

        except:
            pass

    for _, group in md5_map.items():

        if len(group) <= 1:
            continue

        scores = {
            p: get_image_quality_score(p)
            for p in group
        }

        keep = max(scores, key=scores.get)

        for p in group:

            if p != keep:

                moved_path = move_to_duplicates(
                    p,
                    directory,
                    duplicates_dir
                )

                print(f"\n[EXACT DUPLICATE]")
                print(f"KEEP : {keep}")
                print(f"MOVED: {moved_path}")

                moved.add(p)

    # ========================================================
    # STEP 2 — VISUAL HASHING
    # ========================================================

    print("\nSTEP 2: Building visual index...\n")

    remaining = [
        p for p in image_files
        if p not in moved
    ]

    hash_db = {}

    for path in tqdm(remaining):
        hash_db[path] = generate_hashes(path)

    # ========================================================
    # STEP 3 — SIMILARITY MATCHING
    # ========================================================

    print("\nSTEP 3: Detecting visually similar images...\n")

    checked = set()

    for i in range(len(remaining)):

        p1 = remaining[i]

        if p1 in moved:
            continue

        for j in range(i + 1, len(remaining)):

            p2 = remaining[j]

            if p2 in moved:
                continue

            pair_key = tuple(sorted([p1, p2]))

            if pair_key in checked:
                continue

            checked.add(pair_key)

            # ------------------------------------------------
            # Quick perceptual hash filter
            # ------------------------------------------------

            similar_hash = False

            for h1 in hash_db[p1]:

                for h2 in hash_db[p2]:

                    phash_dist = (
                        imagehash.hex_to_hash(h1[0]) -
                        imagehash.hex_to_hash(h2[0])
                    )

                    if phash_dist <= 8:
                        similar_hash = True
                        break

                if similar_hash:
                    break

            if not similar_hash:
                continue

            # ------------------------------------------------
            # ORB verification
            # ------------------------------------------------

            similarity = orb_similarity(p1, p2)

            if similarity >= similarity_threshold:

                score1 = get_image_quality_score(p1)
                score2 = get_image_quality_score(p2)

                keep = p1 if score1 >= score2 else p2
                remove = p2 if keep == p1 else p1

                moved_path = move_to_duplicates(
                    remove,
                    directory,
                    duplicates_dir
                )

                print(f"\n[SIMILAR IMAGE]")
                print(f"Similarity : {similarity:.2f}")
                print(f"KEEP       : {keep}")
                print(f"MOVED      : {moved_path}")

                moved.add(remove)

    print("\n===================================")
    print("DONE")
    print(f"Moved duplicates : {len(moved)}")
    print(f"Duplicates folder: {duplicates_dir}")
    print("===================================\n")


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "directory",
        help="Directory containing images"
    )

    parser.add_argument(
        "--duplicates-dir",
        default="DUPLICATES",
        help="Folder to move duplicates into"
    )

    parser.add_argument(
        "--similarity",
        type=float,
        default=0.85,
        help="Similarity threshold"
    )

    args = parser.parse_args()

    deduplicate(
        directory=args.directory,
        duplicates_dir=args.duplicates_dir,
        similarity_threshold=args.similarity
    )