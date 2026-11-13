import os
import time
import uuid
from typing import List, Dict, Any

from qdrant_client import QdrantClient
from qdrant_client import models
from qdrant_client.models import Distance, VectorParams, PointStruct, TextIndexParams, Filter, FieldCondition, MatchValue, FilterSelector, SparseVectorParams

from config import (
    VECTOR_SIZE,
    QDRANT_COLLECTION,
    QDRANT_HOST,
    QDRANT_PORT,
    CHUNKS_DIR,
    BM25_LANGUAGE,
    embedding_semaphore,
)
from logger import logger
from metadata import mark_as_embedded, acknowledge_pending_deletion_by_doc
from chunking import move_chunks_from_pending


class DeletionFailedError(RuntimeError):
    """Raised when a Qdrant point deletion could not be verified as complete.

    API error, nonzero remaining count or verification failure all land here.
    The durable pending-deletion ledger entry is NOT acknowledged, so the next
    run retries the deletion (defect 2 fix: failure, not warning).
    """


def _delete_doc_verified(client: QdrantClient, collection_name: str, doc_id: int):
    """Delete all points for doc_id and VERIFY zero points remain.

    Raises DeletionFailedError on API failure or nonzero remaining count.
    """
    try:
        client.delete(
            collection_name=collection_name,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[
                        FieldCondition(
                            key="doc_id",
                            match=MatchValue(
                                value=int(doc_id)  # Ensure this is an integer to match stored type
                            )
                        )
                    ]
                )
            ),
            wait=True
        )
    except Exception as e:
        raise DeletionFailedError(
            f"Qdrant delete API failed for doc_id {doc_id} in collection {collection_name}: {e}"
        ) from e

    # VERIFY DELETION: check if any points remain after deletion
    try:
        remaining_points = client.count(
            collection_name=collection_name,
            count_filter=Filter(
                must=[
                    FieldCondition(
                        key="doc_id",
                        match=MatchValue(value=int(doc_id))
                    )
                ]
            )
        ).count
    except Exception as verify_e:
        raise DeletionFailedError(
            f"Could not verify deletion for doc_id {doc_id} in collection "
            f"{collection_name}: {verify_e}"
        ) from verify_e

    if remaining_points > 0:
        raise DeletionFailedError(
            f"{remaining_points} points still exist for doc_id {doc_id} "
            f"after deletion in collection {collection_name}"
        )
    logger.log(f"Verified deletion of all points for doc_id {doc_id} in collection {collection_name}")


def _create_collection_with_indexes(client: QdrantClient, collection_name: str):
    """Create a Qdrant collection with vector config and payload indexes.

    Consolidates the collection creation + index setup that was previously
    triplicated across vector-mismatch-recreate, new-collection, and
    force-reindex code paths.
    """
    client.create_collection(
        collection_name=collection_name,
        vectors_config={
            "dense": VectorParams(
                size=VECTOR_SIZE,
                distance=Distance.COSINE,
                hnsw_config={
                    "m": 16,
                    "ef_construct": 100,
                    "full_scan_threshold": 10000,
                },
                quantization_config=None,
            ),
        },
        sparse_vectors_config={
            "sparse": SparseVectorParams(
                modifier=models.Modifier.IDF,
            )
        },
        hnsw_config={
            "m": 16,
            "ef_construct": 100,
            "full_scan_threshold": 10000,
        },
        optimizers_config={
            "memmap_threshold": 20000,
            "indexing_threshold": 20000,
        }
    )

    # Create text index for BM25 search
    logger.log("Creating text index for sparse search...")
    try:
        client.create_payload_index(
            collection_name=collection_name,
            field_name="text",
            field_schema=TextIndexParams(
                type="text",
                tokenizer="word",
                min_token_len=2,
                max_token_len=20,
                lowercase=True,
            )
        )
        logger.log("Text index created successfully")
    except Exception as e:
        logger.log(f"Warning: Failed to create text index: {e}", "WARNING")
        logger.log("Text search may not work properly", "WARNING")

    # Create payload indexes for better filtering performance
    logger.log("Creating payload indexes for doc_id and source...")
    try:
        client.create_payload_index(
            collection_name=collection_name,
            field_name="doc_id",
            field_schema="integer"
        )
        client.create_payload_index(
            collection_name=collection_name,
            field_name="source",
            field_schema="keyword"
        )
        logger.log("Payload indexes created successfully")
    except Exception as e:
        logger.log(f"Warning: Failed to create payload indexes: {e}", "WARNING")


def update_qdrant_index(new_chunks: List[Dict[str, Any]], changed_doc_ids_by_collection: Dict[str, List[int]], embeddings):
    """Update Qdrant index incrementally for multiple collections.
    
    This function:
    1. Generates embeddings for chunks
    2. Upserts vectors to Qdrant
    3. On success, moves chunks from .pending to main directory
    4. Marks files as successfully embedded
    
    Returns:
        List of file keys (paths) that were successfully embedded
    """
    # Track chunk_ids actually upserted, per source file
    upserted_chunk_ids: dict[str, set[int]] = {}
    
    # Increase timeout to handle larger processing times
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, timeout=300)

    # Group chunks by collection
    chunks_by_collection: dict[str, list] = {}
    for chunk in new_chunks:
        collection_name = chunk.get("collection_name", QDRANT_COLLECTION)  # Default to main collection if not specified
        if collection_name not in chunks_by_collection:
            chunks_by_collection[collection_name] = []
        chunks_by_collection[collection_name].append(chunk)

    # DEFECT 1 FIX: process the UNION of collections that have new chunks and
    # collections that only have deletions. A deletion-only collection (no new
    # chunks) must still reach the deletion loop below.
    all_collections = set(chunks_by_collection.keys()) | set(changed_doc_ids_by_collection.keys())
    # Deterministic order for reproducible runs and tests.
    ordered_collections = sorted(all_collections)

    for collection_name in ordered_collections:
        collection_chunks = chunks_by_collection.get(collection_name, [])
        # Check for force reindex environment variable
        force_reindex = os.getenv("FORCE_REINDEX", "false").lower() == "true"

        # Ensure collection exists
        collections = client.get_collections()
        collection_names = [collection.name for collection in collections.collections]

        if collection_name in collection_names:
            logger.log(f"Using existing Qdrant collection: {collection_name}")
            collection_info = client.get_collection(collection_name)
            # Check if vector configuration matches expected values
            dense_config = collection_info.config.params.vectors.get("dense")
            if not dense_config or dense_config.size != VECTOR_SIZE:
                if force_reindex:
                    logger.log(f"Vector configuration mismatch, recreating collection {collection_name}", "WARNING")
                else:
                    # Plan P1: ordinary reconciliation must NEVER delete/recreate
                    # a collection. A vector-config mismatch is an operator
                    # decision — abort loudly instead of silently wiping data.
                    raise RuntimeError(
                        f"Vector configuration mismatch for collection {collection_name} "
                        f"(expected dense size {VECTOR_SIZE}). Refusing to delete/recreate "
                        "during ordinary reconciliation; run with FORCE_REINDEX=true to rebuild."
                    )
        else:
            logger.log(f"Creating new Qdrant collection: {collection_name}")
            try:
                _create_collection_with_indexes(client, collection_name)
            except Exception as e:
                # Handle the case where the collection was created by another process
                if "already exists" in str(e):
                    logger.log(f"Collection {collection_name} already exists, continuing...", "INFO")
                else:
                    logger.log(f"Failed to create collection {collection_name}: {e}", "ERROR")
                    continue  # Continue with other collections

        # Determine which doc ids to remove for THIS collection from the passed map
        collection_changed_doc_ids = changed_doc_ids_by_collection.get(collection_name, [])

        # Force reindex is an explicit operator action; it may delete/recreate.
        # Ordinary reconciliation must NEVER delete/recreate a collection
        # (plan P1: "no collection delete/recreate in ordinary reconciliation").
        if force_reindex:
            logger.log(f"Force reindex enabled: Clearing all points from collection {collection_name}...")
            try:
                # Most efficient way to clear entire collection: delete and recreate
                # This avoids potential issues with filtering and ensures complete cleanup
                logger.log(f"Deleting collection {collection_name} for reindexing...")
                client.delete_collection(collection_name=collection_name)
                logger.log(f"Collection {collection_name} deleted successfully")

                # Recreate the collection with the same configuration
                logger.log(f"Recreating collection {collection_name}...")
                _create_collection_with_indexes(client, collection_name)

                logger.log(f"Collection {collection_name} recreated successfully for reindexing")
            except Exception as e:
                error_msg = f"Failed to clear collection {collection_name} by recreation: {e}"
                logger.log(error_msg, "ERROR")
                raise RuntimeError(error_msg) from e
        else:
            # Remove old points for changed documents (incremental update)
            if collection_changed_doc_ids:
                logger.log(f"Removing old vectors for {len(collection_changed_doc_ids)} changed documents from collection {collection_name}...")
                for doc_id in collection_changed_doc_ids:
                    # DEFECT 2 FIX: verified two-phase deletion. The ledger entry
                    # was already persisted by metadata.py BEFORE the path map was
                    # dropped. Here we delete, verify zero remaining points, and
                    # only then acknowledge (remove) the ledger entry. Any API
                    # error, nonzero remaining count or verification failure raises
                    # DeletionFailedError so the run fails and the ledger entry
                    # survives for the next run's retry.
                    try:
                        _delete_doc_verified(client, collection_name, doc_id)
                        # Verified success: acknowledge the durable intent.
                        acknowledge_pending_deletion_by_doc(collection_name, doc_id)
                    except DeletionFailedError:
                        raise
                    except Exception as e:
                        raise DeletionFailedError(
                            f"Failed to delete doc_id {doc_id} in collection {collection_name}: {e}"
                        ) from e

        # Add new points to this collection
        if collection_chunks:
            # Optimize batch size based on available memory and performance with new server settings
            batch_size = int(os.getenv("QDRANT_BATCH_SIZE", 64))  # Increased for better performance
            logger.log(f"Processing {len(collection_chunks)} new chunks with batch size {batch_size} in collection {collection_name}")

            successful_points = 0
            failed_chunks = 0

            # Process chunks in batches for better performance
            for i in range(0, len(collection_chunks), batch_size):
                batch = collection_chunks[i:i + batch_size]
                batch_number = (i // batch_size) + 1
                total_batches = (len(collection_chunks) + batch_size - 1) // batch_size

                # Add retry mechanism for each batch
                max_retries = 3
                retry_count = 0

                while retry_count < max_retries:
                    try:
                        # Extract texts for batch embedding
                        texts = [chunk["text"] for chunk in batch]

                        # Skip extremely long chunks in batch
                        filtered_texts = []
                        filtered_chunks = []
                        for j, text in enumerate(texts):
                            if len(text) <= 10000:  # Limit text length
                                filtered_texts.append(text)
                                filtered_chunks.append(batch[j])
                            else:
                                logger.log(f"Skipping extremely long chunk ({len(text)} chars) in batch {batch_number}/{total_batches} in collection {collection_name}", "WARNING")
                                failed_chunks += 1

                        if not filtered_texts:
                            break  # All chunks in batch were too long

                        # Generate embeddings for the batch with semaphore to limit concurrent requests
                        with embedding_semaphore:
                            dense_vectors = embeddings.embed_documents(filtered_texts)

                        # Validate vector sizes
                        valid_chunks = []
                        valid_dense_vectors = []
                        for j, (chunk, vector) in enumerate(zip(filtered_chunks, dense_vectors)):
                            if len(vector) == VECTOR_SIZE:
                                valid_chunks.append(chunk)
                                valid_dense_vectors.append(vector)
                            else:
                                logger.log(f"Invalid vector size for chunk {chunk['chunk_id']} in batch {batch_number}/{total_batches} in collection {collection_name}", "WARNING")
                                failed_chunks += 1

                        if not valid_chunks:
                            break  # No valid chunks in batch

                        bm25_options = {"language": BM25_LANGUAGE.lower()} if BM25_LANGUAGE and BM25_LANGUAGE.lower() != "none" else None

                        # Generate sparse vectors and create points
                        points = []
                        for chunk, dense_vector in zip(valid_chunks, valid_dense_vectors):
                            point = PointStruct(
                                id=str(uuid.uuid4()),
                                vector={
                                    "dense": dense_vector,
                                    "sparse": models.Document(
                                        text=chunk["text"],
                                        model="qdrant/bm25",
                                        options=bm25_options,
                                    )
                                },
                                payload={
                                    "text": chunk["text"],
                                    "doc_id": chunk["doc_id"],
                                    "chunk_id": chunk["chunk_id"],
                                    "source": chunk["source"],
                                    "text_length": len(chunk["text"])
                                }
                            )
                            points.append(point)

                        # Batch upsert points
                        client.upsert(
                            collection_name=collection_name,
                            points=points,
                            wait=True
                        )

                        successful_points += len(points)
                        logger.log(f"Successfully upserted batch {batch_number}/{total_batches} ({len(points)} points) in collection {collection_name}")
                        
                        # Track chunk_ids actually upserted per source file
                        for chunk in valid_chunks:
                            src = chunk['source']
                            upserted_chunk_ids.setdefault(src, set()).add(chunk['chunk_id'])

                        break  # Success, exit retry loop

                    except Exception as e:
                        retry_count += 1
                        if retry_count < max_retries:
                            wait_time = min(5 * (2 ** retry_count), 60)  # Exponential backoff (capped at 60s)
                            logger.log(f"Error processing batch {batch_number}/{total_batches} in collection {collection_name} (attempt {retry_count}): {e}. Retrying in {wait_time}s...", "WARNING")
                            time.sleep(wait_time)
                        else:
                            failed_chunks += len(batch)
                            logger.log(f"Failed to process batch {batch_number}/{total_batches} in collection {collection_name} after {max_retries} attempts: {e}", "ERROR")
                            break  # Exit retry loop on final failure

            logger.log(f"Completed upserting for collection {collection_name}. Successful points: {successful_points}, Failed chunks: {failed_chunks}")
            
            # Determine which sources are fully embedded (all expected chunk_ids present)
            expected_by_source: dict[str, set[int]] = {}
            for chunk in collection_chunks:
                expected_by_source.setdefault(chunk['source'], set()).add(chunk['chunk_id'])

            fully_embedded = [
                src for src, ids in expected_by_source.items()
                if upserted_chunk_ids.get(src, set()) >= ids
            ]

            # Log partially-embedded sources (some chunks upserted but not all)
            for src, expected_ids in expected_by_source.items():
                upserted = upserted_chunk_ids.get(src, set())
                if upserted and not upserted >= expected_ids:
                    # A permanently-failing chunk keeps its file embedded:false and
                    # retries every run — strict completeness over silent gaps.
                    logger.log(
                        f"File not fully embedded, will retry next run: {src} "
                        f"({len(upserted)}/{len(expected_ids)} chunks)",
                        "ERROR",
                    )

            # Move chunks and mark files only for fully-embedded sources
            if fully_embedded:
                fully_embedded_file_keys = list(fully_embedded)
                successful_doc_ids = list(set(
                    chunk['doc_id'] for chunk in collection_chunks
                    if chunk['source'] in fully_embedded
                ))
                if successful_doc_ids:
                    move_chunks_from_pending(successful_doc_ids, path=CHUNKS_DIR, collection_name=collection_name)

                # Mark files as successfully embedded (commit phase)
                mark_as_embedded(fully_embedded_file_keys, collection_name)
