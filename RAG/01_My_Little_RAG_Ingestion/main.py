from pathlib import Path
from tqdm import tqdm

from config import INPUT_DIR, QDRANT_COLLECTION, EXPECTED_SOURCES, MIGRATION_MARKER, validate_required_env
from logger import logger
from metadata import load_documents_incremental
from chunking import (
    chunk_document,
    save_chunks_to_disk,
    get_embedding_function,
    cleanup_pending_chunks,
)
from qdrant_ops import update_qdrant_index


def check_source_readiness():
    """Source-readiness guard (defect 4): refuse to scan when the input tree
    does not look like the expected three-source layout.

    A missing checkout or unmounted input root must be a hard abort, never a
    mass deletion. Checked in the ingestion entry path itself, before any
    scan/deletion diff.
    """
    input_path = Path(INPUT_DIR)
    if not input_path.exists():
        raise RuntimeError(
            f"Input directory '{INPUT_DIR}' does not exist. "
            "Refusing to run: a missing input root must never be read as mass deletion."
        )
    if not any(input_path.iterdir()):
        raise RuntimeError(
            f"Input directory '{INPUT_DIR}' is empty (unmounted volume?). "
            "Refusing to run: an empty input root must never be read as mass deletion."
        )

    marker = input_path / MIGRATION_MARKER
    if marker.exists():
        raise RuntimeError(
            f"Migration marker '{marker}' present: a migration/replacement is "
            "in progress. Ingestion refuses to run against a source tree "
            "under replacement."
        )

    missing = []
    for rel in EXPECTED_SOURCES:
        source = input_path / rel
        if not source.exists() or not any(source.iterdir()):
            missing.append(rel)
    if missing:
        raise RuntimeError(
            "Expected source checkout(s) missing or empty: "
            + ", ".join(missing)
            + ". Refusing to run: this could read as mass deletion."
        )
    logger.log("Source-readiness guard passed")


def main():
    """Main ingestion process"""
    validate_required_env()
    try:
        logger.log("Starting document ingestion")
        logger.log(f"Using collection: {QDRANT_COLLECTION}")

        # DEFECT 4: readiness gate BEFORE any scan / deletion diff.
        check_source_readiness()

        # Clean up any pending chunks from failed previous runs
        logger.log("Cleaning up pending chunks from previous runs...")
        cleanup_pending_chunks()
        logger.log("Pending chunks cleanup completed")

        logger.log("Checking for new or changed documents...")
        new_docs, changed_doc_ids_by_collection = load_documents_incremental()

        if not new_docs and not changed_doc_ids_by_collection:
            logger.log("No changes detected. All documents are up to date!")
            return

        logger.log(f"Found {len(new_docs)} new/changed documents")

        # Chunk new/changed documents
        new_chunks = []
        for doc in tqdm(new_docs, desc="Chunking"):
            new_chunks.extend(chunk_document(doc))

        if new_chunks:
            logger.log(f"Saving {len(new_chunks)} new chunks to disk...")
            # Group chunks by collection for saving
            chunks_by_collection = {}
            for chunk in new_chunks:
                collection_name = chunk.get("collection_name", QDRANT_COLLECTION)  # Default to main collection if not specified
                if collection_name not in chunks_by_collection:
                    chunks_by_collection[collection_name] = []
                chunks_by_collection[collection_name].append(chunk)

            # Save chunks to their respective collection directories
            for collection_name, collection_chunks in chunks_by_collection.items():
                logger.log(f"Saving {len(collection_chunks)} chunks to collection {collection_name}...")
                save_chunks_to_disk(collection_chunks, collection_name=collection_name)

        # DEFECT 1: deletion-only runs reach update_qdrant_index via the union
        # of collections in qdrant_ops; they must NOT require an embedding
        # endpoint. Load the model only when there is something to embed.
        embeddings = None
        if new_chunks:
            logger.log("Loading embedding model...")
            embeddings = get_embedding_function()

        logger.log("Updating Qdrant index...")
        update_qdrant_index(new_chunks, changed_doc_ids_by_collection, embeddings)

        logger.log("Incremental indexing complete!")
    except Exception as e:
        logger.log(f"Fatal error: {e}", "ERROR")
        raise
    finally:
        # Print error/warning summary
        logger.print_summary()

if __name__ == "__main__":
    main()
