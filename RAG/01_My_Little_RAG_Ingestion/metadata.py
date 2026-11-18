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
    PENDING_DELETIONS_FILE,
    EXCLUDED_DIR_NAMES,
    EXCLUDED_FILENAMES,
    MASS_REMOVAL_THRESHOLD,
    MASS_REMOVAL_MIN_COUNT,
    MASS_REMOVAL_MANIFEST,
)
from logger import logger
from chunking import remove_yaml_front_matter


class MassRemovalError(RuntimeError):
    """Raised when the mass-removal guard detects suspicious bulk deletion.

    Signals a missing checkout / incomplete source replacement rather than
    intentional deletion; the run aborts before any Qdrant deletion.
    """


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

# --- Pending-deletion ledger (durable, two-phase deletion commit) ---
# Entries: {"<collection>:<file_key>": {"doc_id": int, "collection": str,
#            "file": str, "requested_at": iso8601}}
# The ledger is persisted BEFORE path-map entries are dropped, so a crash or
# failed Qdrant deletion always leaves retryable state. An entry is removed
# ONLY after Qdrant deletion is verified (zero remaining points).

def load_pending_deletions() -> Dict[str, Dict[str, Any]]:
    """Load the durable pending-deletion ledger from disk."""
    try:
        with open(PENDING_DELETIONS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.log(f"Could not load pending-deletion ledger: {e}", "ERROR")
        return {}

def record_pending_deletion(collection_name: str, file_key: str, doc_id: int):
    """Persist deletion intent durably BEFORE dropping metadata/chunk mappings."""
    ledger = load_pending_deletions()
    ledger[f"{collection_name}:{file_key}"] = {
        "doc_id": int(doc_id),
        "collection": collection_name,
        "file": file_key,
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        os.makedirs(os.path.dirname(PENDING_DELETIONS_FILE), exist_ok=True)
        temp_file = f"{PENDING_DELETIONS_FILE}.tmp"
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, PENDING_DELETIONS_FILE)
        logger.log(f"Recorded pending deletion: {collection_name}:{file_key} (doc_id {doc_id})")
    except Exception as e:
        # Failing to persist intent is fatal: proceeding would risk losing the
        # deletion (defect 2). Raise so the run aborts before any Qdrant call.
        raise RuntimeError(
            f"Could not persist pending-deletion ledger entry for "
            f"{collection_name}:{file_key}: {e}"
        )

def acknowledge_pending_deletion(collection_name: str, file_key: str):
    """Remove a ledger entry AFTER verified Qdrant deletion success."""
    ledger = load_pending_deletions()
    key = f"{collection_name}:{file_key}"
    if key in ledger:
        del ledger[key]
        _save_pending_deletions(ledger)

def acknowledge_pending_deletion_by_doc(collection_name: str, doc_id: int):
    """Acknowledge every ledger entry for (collection, doc_id).

    qdrant_ops only knows (collection, doc_id) at deletion time; the file key
    lives in the ledger entry itself. Removes all matching entries (a doc_id
    is unique per collection).
    """
    target = int(doc_id)
    ledger = load_pending_deletions()
    remaining = {}
    removed = 0
    for key, entry in ledger.items():
        if entry.get("collection") == collection_name and int(entry.get("doc_id", -1)) == target:
            removed += 1
            continue
        remaining[key] = entry
    if removed:
        _save_pending_deletions(remaining)
        logger.log(f"Acknowledged {removed} pending deletion(s) for {collection_name} doc_id {target}")

def _save_pending_deletions(ledger: Dict[str, Dict[str, Any]]):
    try:
        os.makedirs(os.path.dirname(PENDING_DELETIONS_FILE), exist_ok=True)
        temp_file = f"{PENDING_DELETIONS_FILE}.tmp"
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, PENDING_DELETIONS_FILE)
    except Exception as e:
        # Failing to persist the ledger is fatal: an unacknowledged entry that
        # silently vanishes would lose the deletion retry state (defect 2).
        raise RuntimeError(f"Could not persist pending-deletion ledger: {e}") from e

def is_path_excluded(path: Path) -> bool:
    """True for repo internals / administrative documents that must never be ingested.

    Excluded: any path component named in EXCLUDED_DIR_NAMES (e.g. .git) and
    files whose name is in EXCLUDED_FILENAMES (e.g. README.md) at any depth.
    Tolerates both absolute paths and input-relative keys ('input/...').
    """
    try:
        parts = path.relative_to(Path(INPUT_DIR)).parts
    except ValueError:
        # Not under INPUT_DIR (e.g. stored relative key 'input/...'):
        # strip a leading 'input' component if present and use the rest.
        parts = path.parts[1:] if path.parts and path.parts[0] == "input" else path.parts
    if any(part in EXCLUDED_DIR_NAMES for part in parts[:-1]):
        return True
    return path.name in EXCLUDED_FILENAMES

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
            doc_id = int(chunk_file.stem.split('_')[0])
            doc_ids.add(doc_id)
        except (ValueError, IndexError):
            logger.log(f"Could not parse doc_id from filename {chunk_file.name}", "WARNING")
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
    try:
        relative_path = file_path.relative_to(INPUT_DIR)
    except ValueError:
        # Tolerate stored input-relative keys ('input/...') alongside absolute paths
        parts = file_path.parts[1:] if file_path.parts and file_path.parts[0] == "input" else file_path.parts
        relative_path = Path(*parts) if parts else Path()
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

    # Pre-existing metadata entries for paths the scanner no longer visits
    # (excluded repo internals / admin docs) are carried forward unchanged —
    # exclusion must never silently purge already-persisted state.
    # The deleted-file diff below runs against scanned files only.

    # Group files by collection first
    files_by_collection = {}
    for path in Path(INPUT_DIR).rglob("*"):
        if path.suffix not in [".txt", ".md", ".html"]:
            continue

        # Explicit exclusion of repo internals and administrative documents.
        if is_path_excluded(path):
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
    stored_path_maps = stored_metadata.setdefault('_path_maps', {})
    for collection_name in files_by_collection.keys():
        if collection_name in stored_path_maps and stored_path_maps[collection_name]:
            # Use persisted map — skip the expensive disk scan
            for source_path, doc_id in stored_path_maps[collection_name].items():
                key = f"{collection_name}:{source_path}"
                all_path_to_doc_id[key] = {
                    'doc_id': doc_id,
                    'collection': collection_name
                }
        else:
            # No persisted map for this collection — scan chunks on disk
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

            # Backfill _path_maps from the scan result
            path_map = {}
            for map_key, val in all_path_to_doc_id.items():
                if map_key.startswith(f"{collection_name}:"):
                    path_map[map_key[len(collection_name) + 1:]] = val['doc_id']
            stored_path_maps[collection_name] = path_map

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
                # Transiently unreadable: carry the stored entry over unchanged so the
                # deleted-diff below does not classify this file as deleted (B4).
                if file_key in stored_metadata:
                    current_metadata[file_key] = stored_metadata[file_key]
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
                    # Record in _path_maps so an interrupted run keeps the mapping
                    stored_path_maps.setdefault(collection_name, {})[file_key] = doc_id

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

    # Persist path maps so subsequent runs can skip chunk-dir scans.
    # FORCE_REINDEX: clears Qdrant collections but not the chunks dir, so the map stays valid.
    path_maps = stored_metadata.get('_path_maps', {})
    if path_maps:
        current_metadata.setdefault('_path_maps', {}).update(path_maps)

    # Check for deleted files (only if not reindexing)
    if not force_reindex:
        deleted_files = set(stored_metadata.keys()) - set(current_metadata.keys())
        # Internal bookkeeping keys ('_path_maps', '_counters') are not files.
        deleted_files = {k for k in deleted_files if not k.startswith('_')}

        # Phase 1 — classify each disappearance WITHOUT mutating anything.
        # confirmed_gone = files that are excluded from processing, stat-verified
        # gone, and not protected by the guards below.
        confirmed_gone = []
        for deleted_file in sorted(deleted_files):
            # Exclusion semantics: a path that is now excluded (repo internals,
            # admin docs) is NOT treated as deleted — its metadata is carried
            # forward unchanged so pre-existing excluded entries are preserved.
            if is_path_excluded(Path(deleted_file)):
                current_metadata[deleted_file] = stored_metadata[deleted_file]
                logger.log(f"Path now excluded, metadata preserved, not evicting: {deleted_file}", "INFO")
                continue
            # Guard: verify the file is truly gone before evicting.
            # Do NOT use Path.exists() — it swallows OSError and returns False,
            # which would re-open the exact bug class being fixed (B4).
            try:
                os.stat(deleted_file)
            except FileNotFoundError:
                confirmed_gone.append(deleted_file)
            except OSError as e:
                # Other stat failure (e.g. permissions): assume alive
                current_metadata[deleted_file] = stored_metadata[deleted_file]
                logger.log(f"Path unreadable, not evicting: {deleted_file} ({e})", "WARNING")
                continue
            else:
                # stat succeeded — file exists but was not scanned this run (e.g. rglob missed it)
                current_metadata[deleted_file] = stored_metadata[deleted_file]
                logger.log(f"Path exists but was not scanned this run, not evicting: {deleted_file}", "WARNING")
                continue

        # Source-readiness: mass-removal guard. If a large fraction of the
        # stored library disappeared this run, treat it as a missing source /
        # incomplete replacement rather than intentional deletion: abort with
        # a preview BEFORE any Qdrant deletion, ledger entry or metadata
        # update (defect 4). Intentional large deletion requires a scoped
        # reviewed manifest passed via MASS_REMOVAL_MANIFEST.
        stored_count = len([k for k in stored_metadata if not k.startswith('_')])
        removed_count = len(confirmed_gone)
        allowance = max(MASS_REMOVAL_THRESHOLD * stored_count, MASS_REMOVAL_MIN_COUNT) if stored_count else 0
        if removed_count > allowance:
            manifest_prefixes = []
            if MASS_REMOVAL_MANIFEST and os.path.exists(MASS_REMOVAL_MANIFEST):
                with open(MASS_REMOVAL_MANIFEST, 'r', encoding='utf-8') as mf:
                    manifest_prefixes = [
                        line.strip() for line in mf if line.strip() and not line.startswith('#')
                    ]
            unmanifested = [
                f for f in confirmed_gone
                if not any(f.startswith(p) for p in manifest_prefixes)
            ]
            if unmanifested:
                preview = "\n  ".join(unmanifested[:20])
                more = f"\n  … and {len(unmanifested) - 20} more" if len(unmanifested) > 20 else ""
                logger.log(
                    f"MASS-REMOVAL GUARD: {removed_count} of {stored_count} files disappeared "
                    f"(allowance {allowance:.0f}). This looks like a missing checkout or "
                    f"an incomplete source replacement, not routine deletion. "
                    f"Aborting before any Qdrant deletion. Preview of unmanifested removals:\n  "
                    f"{preview}{more}",
                    "ERROR",
                )
                raise MassRemovalError(
                    f"{removed_count} files removed (allowance {allowance:.0f}); "
                    f"{len(unmanifested)} not covered by a reviewed manifest"
                )

        # Phase 2 — process confirmed deletions. All files in confirmed_gone
        # are stat-verified absent and below the mass-removal threshold.
        for deleted_file in confirmed_gone:
            logger.log(f"File deleted: {deleted_file}")
            deleted_file_path = Path(deleted_file)
            collection_name = get_collection_name_from_path(deleted_file_path)

            # Find the doc_id from the specific collection's chunks (both main
            # and .pending). O1: map hit avoids the full collection rescan;
            # scan only backfills first-run-after-upgrade.
            mapped_doc_id = stored_path_maps.get(collection_name, {}).get(deleted_file)
            if mapped_doc_id is not None:
                path_to_doc_id = {deleted_file: mapped_doc_id}
            else:
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
                # DURABILITY (defect 2): persist deletion intent BEFORE dropping
                # the path-map entry or removing chunk files. Acknowledgement
                # happens only after verified Qdrant success (qdrant_ops).
                record_pending_deletion(collection_name, deleted_file, doc_id_to_remove)
                # Record for deletion BEFORE removing chunk files (avoid timing issue)
                changed_doc_ids_by_collection[collection_name].add(int(doc_id_to_remove))
                remove_old_chunks(doc_id_to_remove, chunks_dir=CHUNKS_DIR, collection_name=collection_name)
                stored_path_maps.get(collection_name, {}).pop(deleted_file, None)
                logger.log(f"Marked doc_id {doc_id_to_remove} for removal from collection {collection_name}")

    # DEFECT 2 retry: ledger entries persisted but never acknowledged (crash
    # or failure before verified Qdrant deletion) are retried on every run,
    # independent of the current scan diff — after a failed run the file key
    # is already gone from stored metadata, so the scan diff alone would never
    # re-produce the deletion.
    for ledger_key, entry in load_pending_deletions().items():
        ledger_collection = entry.get("collection")
        try:
            ledger_doc_id = int(entry.get("doc_id", -1))
        except (TypeError, ValueError):
            logger.log(f"Skipping malformed pending-deletion ledger entry: {ledger_key}", "WARNING")
            continue
        if ledger_collection and ledger_doc_id >= 0:
            changed_doc_ids_by_collection[ledger_collection].add(ledger_doc_id)

    # Save updated metadata
    save_file_metadata(current_metadata)

    # Convert sets to sorted lists for deterministic output
    changed_doc_ids_by_collection_final = {k: sorted(list(v)) for k, v in changed_doc_ids_by_collection.items()}

    return new_docs, changed_doc_ids_by_collection_final
