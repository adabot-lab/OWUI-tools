import os
import json
import hashlib
import time
import re
from pathlib import Path
from typing import List, Dict, Any, Optional
from collections import defaultdict
from bs4 import BeautifulSoup

from config import (
    INPUT_DIR,
    CHUNKS_DIR,
    METADATA_FILE,
    QDRANT_COLLECTION,
)
from logger import logger
from chunking import remove_yaml_front_matter


def load_file_metadata() -> Dict[str, Any]:
    """Load file metadata from disk"""
    if not os.path.exists(METADATA_FILE):
        return {}

    try:
        with open(METADATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.log(f"Error loading metadata: {e}", "ERROR")
        return {}

def save_file_metadata(metadata: Dict[str, Any]):
    """Save metadata with atomic write"""
    try:
        os.makedirs(os.path.dirname(METADATA_FILE), exist_ok=True)
        temp_file = f"{METADATA_FILE}.tmp"
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, METADATA_FILE)
    except Exception as e:
        logger.log(f"Error saving metadata: {e}", "ERROR")

def mark_as_embedded(file_keys: List[str], collection_name: str):
    """Mark files as successfully embedded (commit phase).
    
    This is the commit point - files are only considered processed after this succeeds.
    
    Args:
        file_keys: List of file paths (keys) to mark as embedded
        collection_name: Collection name
    """
    metadata = load_file_metadata()
    updated = False
    
    for file_key in file_keys:
        if file_key in metadata and collection_name in metadata[file_key].get('collections', {}):
            metadata[file_key]['collections'][collection_name]['embedded'] = True
            updated = True
    
    if updated:
        save_file_metadata(metadata)
        logger.log(f"Marked {len(file_keys)} files as successfully embedded for collection {collection_name}", "INFO")

def get_file_hash(file_path: str) -> str:
    """Get SHA256 hash of file content"""
    hash_sha256 = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_sha256.update(chunk)
        return hash_sha256.hexdigest()
    except Exception as e:
        logger.log(f"Error hashing file {file_path}: {e}", "ERROR")
        return ""

def has_file_changed(file_path: str, stored_metadata: Dict[str, Any], collection_name: str, current_hash: Optional[str] = None) -> bool:
    """Check if file has changed, was processed with a different collection, or has incomplete embedding"""
    file_key = str(file_path)
    if file_key not in stored_metadata:
        return True

    # Check if file was processed with a different collection
    if collection_name not in stored_metadata[file_key].get('collections', {}):
        return True

    # Check if embedding is incomplete (missing or false)
    collection_metadata = stored_metadata[file_key]['collections'][collection_name]
    embedded = collection_metadata.get('embedded', False)
    if not embedded:
        return True

    try:
        current_mtime = os.path.getmtime(file_path)
        if current_hash is None:
            current_hash = get_file_hash(file_path)
        stored_mtime = collection_metadata.get('mtime', 0)
        stored_hash = collection_metadata.get('hash', '')

        return (abs(current_mtime - stored_mtime) > 1 or current_hash != stored_hash)
    except Exception as e:
        logger.log(f"Error checking file changes {file_path}: {e}", "ERROR")
        return True

def remove_old_chunks(doc_id: int, chunks_dir: str = CHUNKS_DIR, collection_name: str = None) -> int:
    """Move old chunks for a specific document into a .removed folder (returns moved count).
    
    Also removes chunks from .pending directory if they exist.
    """
    if collection_name is None:
        collection_name = QDRANT_COLLECTION  # Maintain backward compatibility
    collection_chunks_dir = os.path.join(chunks_dir, collection_name)
    removed_dir = os.path.join(collection_chunks_dir, ".removed")
    pending_dir = os.path.join(collection_chunks_dir, ".pending")
    os.makedirs(removed_dir, exist_ok=True)

    pattern = f"{doc_id}_*.json"
    moved = 0
    errors = 0

    # Remove from main directory
    for chunk_file in Path(collection_chunks_dir).glob(pattern):
        if ".pending" in str(chunk_file) or ".removed" in str(chunk_file):
            continue
        try:
            dest = Path(removed_dir) / chunk_file.name
            # If a file with same name exists in removed, add a suffix
            if dest.exists():
                dest = dest.with_name(f"{chunk_file.stem}_{int(time.time())}{chunk_file.suffix}")
            chunk_file.rename(dest)
            moved += 1
        except Exception as e:
            logger.log(f"Could not move old chunk {chunk_file} to .removed: {e}", "WARNING")
            errors += 1
    
    # Remove from .pending directory (chunks that were never embedded)
    if os.path.exists(pending_dir):
        for chunk_file in Path(pending_dir).glob(pattern):
            try:
                chunk_file.unlink()
                moved += 1
            except Exception as e:
                logger.log(f"Could not remove pending chunk {chunk_file}: {e}", "WARNING")
                errors += 1

    if moved or errors:
        logger.log(f"Moved {moved} old chunks (errors: {errors}) for doc_id {doc_id} in collection {collection_name}", "INFO")

    return moved

def get_existing_doc_ids(collection_name: str = None) -> set:
    """Get all existing document IDs from chunks (excluding .pending directory)"""
    doc_ids = set()
    # Use collection-specific subfolder
    if collection_name is None:
        collection_name = QDRANT_COLLECTION  # Maintain backward compatibility
    collection_chunks_dir = os.path.join(CHUNKS_DIR, collection_name)
    for chunk_file in Path(collection_chunks_dir).glob("*.json"):
        # Skip .pending directory
        if ".pending" in str(chunk_file):
            continue
        try:
            with open(chunk_file, 'r', encoding='utf-8') as f:
                chunk = json.load(f)
                if isinstance(chunk, dict) and 'doc_id' in chunk:
                    doc_ids.add(int(chunk['doc_id']))
        except Exception as e:
            logger.log(f"Could not read chunk file {chunk_file}: {e}", "WARNING")
    return doc_ids

def get_next_doc_id(collection_name: str = None, stored_metadata: Dict[str, Any] = None) -> int:
    """Get the next available document ID.

    Reads the cached max doc_id from file_metadata.json (``_counters`` -> collection)
    when available, instead of scanning every chunk JSON on disk. The cached value is
    the highest assigned doc_id for the collection; the next free id is that + 1.
    Seeded by scanning the chunks directory on first use.
    """
    if collection_name is None:
        collection_name = QDRANT_COLLECTION  # Maintain backward compatibility

    # Fast path: use cached max doc_id from metadata if present
    if stored_metadata is not None:
        cached = stored_metadata.get('_counters', {}).get(collection_name)
        if cached is not None:
            return int(cached) + 1

    # Fallback / seeding path: scan chunks directory once to find current max
    existing_ids = get_existing_doc_ids(collection_name)
    max_existing = max(existing_ids, default=0)

    if stored_metadata is not None:
        # Cache the max assigned id (not max+1) so the fast path returns max+1.
        stored_metadata.setdefault('_counters', {})[collection_name] = max_existing
    return max_existing + 1

def get_collection_name_from_path(file_path: Path) -> str:
    """Get collection name from file path based on subfolder, with sanitization"""
    # Get the parent directory relative to INPUT_DIR
    relative_path = file_path.relative_to(INPUT_DIR)
    # If file is directly in input dir (no subfolder), use default collection
    if len(relative_path.parts) <= 1:
        return QDRANT_COLLECTION
    else:
        # Use the first part (immediate subfolder) as collection name
        raw_collection_name = relative_path.parts[0]
        # Sanitize collection name to comply with Qdrant requirements
        # Qdrant collection names should contain lowercase letters, numbers, hyphens, underscores
        sanitized = re.sub(r'[^a-z0-9_-]', '_', raw_collection_name.lower())
        # Ensure it doesn't start with underscore/dash and has appropriate length
        sanitized = sanitized.strip('_-')
        sanitized = sanitized[:255]  # Limit length
        if not sanitized:  # If it becomes empty after sanitization
            sanitized = "default_collection"
        return sanitized

def load_documents_incremental() -> tuple[List[Dict[str, Any]], Dict[str, List[int]]]:
    """Load only new or changed documents based on collection"""
    # Check for force reindex environment variable
    force_reindex = os.getenv("FORCE_REINDEX", "false").lower() == "true"
    if force_reindex:
        logger.log("FORCE_REINDEX is enabled. All documents will be reprocessed.", "INFO")

    stored_metadata = load_file_metadata()
    current_metadata = {}

    # Group files by collection first
    files_by_collection = {}
    for path in Path(INPUT_DIR).rglob("*"):
        if path.suffix not in [".txt", ".md", ".html"]:
            continue

        file_key = str(path)

        # Determine collection name based on subfolder
        collection_name = get_collection_name_from_path(path)

        if collection_name not in files_by_collection:
            files_by_collection[collection_name] = []
        files_by_collection[collection_name].append((path, file_key))

    # Build comprehensive mapping of file paths to doc_ids for ALL collections first
    # Include both main directory and .pending directory to handle failed runs
    all_path_to_doc_id = {}
    for collection_name in files_by_collection.keys():
        collection_chunks_dir = os.path.join(CHUNKS_DIR, collection_name)
        pending_chunks_dir = os.path.join(collection_chunks_dir, ".pending")
        
        # Load from main directory
        if os.path.exists(collection_chunks_dir):
            for chunk_file in Path(collection_chunks_dir).glob("*.json"):
                if ".pending" in str(chunk_file):
                    continue
                try:
                    with open(chunk_file, 'r', encoding='utf-8') as f:
                        chunk = json.load(f)
                        source_path = chunk['source']
                        # Store with collection context to avoid conflicts
                        key = f"{collection_name}:{source_path}"
                        all_path_to_doc_id[key] = {
                            'doc_id': chunk['doc_id'],
                            'collection': collection_name
                        }
                except Exception as e:
                    logger.log(f"Could not read chunk file {chunk_file}: {e}", "WARNING")
        
        # Load from .pending directory (to handle chunks that weren't successfully embedded)
        if os.path.exists(pending_chunks_dir):
            for chunk_file in Path(pending_chunks_dir).glob("*.json"):
                try:
                    with open(chunk_file, 'r', encoding='utf-8') as f:
                        chunk = json.load(f)
                        source_path = chunk['source']
                        # Store with collection context to avoid conflicts
                        key = f"{collection_name}:{source_path}"
                        # If the file is also in the main directory, prefer that (it was successfully embedded)
                        if key not in all_path_to_doc_id:
                            all_path_to_doc_id[key] = {
                                'doc_id': chunk['doc_id'],
                                'collection': collection_name
                            }
                except Exception as e:
                    logger.log(f"Could not read pending chunk file {chunk_file}: {e}", "WARNING")

    new_docs = []
    changed_doc_ids_by_collection: Dict[str, set] = defaultdict(set)

    # Process each collection separately
    for collection_name, file_list in files_by_collection.items():
        # Get doc ID counter for this collection (fast path via metadata cache)
        doc_id_counter = get_next_doc_id(collection_name, stored_metadata=stored_metadata)

        for path, file_key in file_list:
            try:
                current_mtime = os.path.getmtime(path)
                # Cheap path: if stored mtime is unchanged, reuse the stored hash
                # and skip the full file read + SHA256. Only hash when mtime moved.
                stored_collection_meta = (
                    stored_metadata.get(file_key, {})
                    .get('collections', {})
                    .get(collection_name)
                )
                if (
                    not force_reindex
                    and stored_collection_meta
                    and stored_collection_meta.get('embedded')
                    and abs(current_mtime - stored_collection_meta.get('mtime', -1)) <= 1
                ):
                    current_hash = stored_collection_meta.get('hash', '')
                else:
                    current_hash = get_file_hash(path)
            except OSError as e:
                logger.log(f"Could not access file {path}: {e}", "WARNING")
                continue

            # Check if file changed or needs reprocessing for this collection
            # When FORCE_REINDEX is true, process all files regardless of changes
            file_has_changed = has_file_changed(path, stored_metadata, collection_name, current_hash=current_hash)
            should_process_file = force_reindex or file_has_changed

            # Update current metadata
            if file_key not in current_metadata:
                current_metadata[file_key] = {
                    'collections': {}
                }

            # Copy existing collections data if available
            if file_key in stored_metadata and 'collections' in stored_metadata[file_key]:
                current_metadata[file_key]['collections'] = stored_metadata[file_key]['collections'].copy()

            # Only set embedded: false for files that are actually being processed
            # Unchanged files should preserve their existing embedded status
            if should_process_file:
                current_metadata[file_key]['collections'][collection_name] = {
                    'mtime': current_mtime,
                    'hash': current_hash,
                    'embedded': False
                }
            else:
                # File unchanged - update mtime and hash but preserve embedded status
                if collection_name in current_metadata[file_key]['collections']:
                    current_metadata[file_key]['collections'][collection_name]['mtime'] = current_mtime
                    current_metadata[file_key]['collections'][collection_name]['hash'] = current_hash
                else:
                    # Collection not in metadata yet (shouldn't happen but handle gracefully)
                    current_metadata[file_key]['collections'][collection_name] = {
                        'mtime': current_mtime,
                        'hash': current_hash,
                        'embedded': False
                    }

            if should_process_file:
                logger.log(f"Processing {'new' if file_key not in stored_metadata or collection_name not in stored_metadata[file_key].get('collections', {}) else 'changed/re-collection'} file: {path} for collection: {collection_name}")

                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        text = f.read()
                        if path.suffix == ".html":
                            text = BeautifulSoup(text, "html.parser").get_text()

                        # Remove YAML front matter from the document
                        text = remove_yaml_front_matter(text)
                except Exception as e:
                    logger.log(f"Could not read file {path}: {e}", "WARNING")
                    continue

                # CRITICAL FIX: Consistent document ID handling
                collection_key = f"{collection_name}:{file_key}"
                old_doc_id = None

                # Check if this file was previously indexed in THIS collection
                if collection_key in all_path_to_doc_id:
                    old_doc_id = all_path_to_doc_id[collection_key]['doc_id']
                    # Record for deletion BEFORE removing chunk files (avoid timing issue)
                    changed_doc_ids_by_collection[collection_name].add(int(old_doc_id))
                    # Remove old chunks
                    remove_old_chunks(old_doc_id, chunks_dir=CHUNKS_DIR, collection_name=collection_name)
                    # Reuse the same doc_id
                    doc_id = old_doc_id
                else:
                    # Assign new doc_id
                    doc_id = doc_id_counter
                    doc_id_counter += 1

                new_docs.append({
                    "text": text,
                    "source": file_key,
                    "doc_id": doc_id,
                    "collection_name": collection_name  # Add collection name to track where to store this doc
                })

        # Persist the highest assigned doc_id for this collection so the next
        # run reads it from metadata instead of rescanning chunk JSONs.
        # doc_id_counter points one past the last assigned id (when any were
        # allocated this run); cache counter as max_assigned (= counter - 1).
        if doc_id_counter > 0:
            stored_metadata.setdefault('_counters', {})[collection_name] = doc_id_counter - 1

    # Persist the updated max doc_id counters so the next run can read them
    # from metadata instead of rescanning every chunk JSON on disk.
    counters = stored_metadata.get('_counters', {})
    if counters:
        current_metadata.setdefault('_counters', {}).update(counters)

    # Check for deleted files (only if not force reindexing)
    if not force_reindex:
        deleted_files = set(stored_metadata.keys()) - set(current_metadata.keys())
        for deleted_file in deleted_files:
            logger.log(f"File deleted: {deleted_file}")
            # Determine which collection this file belonged to
            deleted_file_path = Path(deleted_file)
            collection_name = get_collection_name_from_path(deleted_file_path)

            # Find and remove chunks for deleted files
            # Need to load doc_id from the specific collection's chunks (both main and .pending)
            path_to_doc_id = {}
            collection_chunks_dir = os.path.join(CHUNKS_DIR, collection_name)
            pending_chunks_dir = os.path.join(collection_chunks_dir, ".pending")
            
            # Check main directory
            if os.path.exists(collection_chunks_dir):
                for chunk_file in Path(collection_chunks_dir).glob("*.json"):
                    if ".pending" in str(chunk_file):
                        continue
                    try:
                        with open(chunk_file, 'r', encoding='utf-8') as f:
                            chunk = json.load(f)
                            source_path = chunk['source']
                            if source_path == deleted_file:
                                path_to_doc_id[source_path] = chunk['doc_id']
                    except Exception as e:
                        logger.log(f"Could not read chunk file {chunk_file}: {e}", "WARNING")
            
            # Check .pending directory
            if os.path.exists(pending_chunks_dir):
                for chunk_file in Path(pending_chunks_dir).glob("*.json"):
                    try:
                        with open(chunk_file, 'r', encoding='utf-8') as f:
                            chunk = json.load(f)
                            source_path = chunk['source']
                            if source_path == deleted_file:
                                # Only add if not already in path_to_doc_id (prefer main directory)
                                if source_path not in path_to_doc_id:
                                    path_to_doc_id[source_path] = chunk['doc_id']
                    except Exception as e:
                        logger.log(f"Could not read pending chunk file {chunk_file}: {e}", "WARNING")

            if deleted_file in path_to_doc_id:
                doc_id_to_remove = path_to_doc_id[deleted_file]
                # Record for deletion BEFORE removing chunk files (avoid timing issue)
                changed_doc_ids_by_collection[collection_name].add(int(doc_id_to_remove))
                remove_old_chunks(doc_id_to_remove, chunks_dir=CHUNKS_DIR, collection_name=collection_name)
                logger.log(f"Marked doc_id {doc_id_to_remove} for removal from collection {collection_name}")

    # Save updated metadata
    save_file_metadata(current_metadata)

    # Convert sets to sorted lists for deterministic output
    changed_doc_ids_by_collection_final = {k: sorted(list(v)) for k, v in changed_doc_ids_by_collection.items()}

    return new_docs, changed_doc_ids_by_collection_final
