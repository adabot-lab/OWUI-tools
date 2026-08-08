import os
import json
import re
import time
from pathlib import Path
from typing import List, Dict, Any

from langchain_openai import OpenAIEmbeddings
from langchain_ollama import OllamaEmbeddings

from config import (
    MAX_CHUNK_SIZE,
    MIN_CHUNK_SIZE,
    CHUNKS_DIR,
    QDRANT_COLLECTION,
    PROVIDER,
    EMBEDDING_MODEL_NAME,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OLLAMA_BASE_URL,
)
from logger import logger

# Optional: tiktoken gives an accurate BPE token count for chunk sizing.
# requirements.txt already pins it; import is guarded so the module still loads
# if the package is unavailable in a minimal environment.
_TIKTOKEN_ENC = None
try:
    import tiktoken as _tiktoken_mod
    _TIKTOKEN_ENC = _tiktoken_mod.get_encoding("cl100k_base")
except Exception as _tiktoken_err:  # pragma: no cover - environment dependent
    logger.log(f"tiktoken unavailable; falling back to heuristic token estimate: {_tiktoken_err}", "WARNING")


def remove_yaml_front_matter(text: str) -> str:
    """Remove YAML front matter from the beginning of documents.

    The YAML front matter is enclosed between '---' lines at the beginning of the document.
    """
    # Pattern to match YAML front matter at the beginning of the document
    # It starts with --- on a line by itself, followed by any content,
    # and ends with --- on a line by itself
    pattern = r'^---\n.*?\n---(?:\n|$)'

    # Use re.DOTALL to match across multiple lines
    result = re.sub(pattern, '', text, count=1, flags=re.DOTALL)
    return result.strip()


def split_into_paragraphs(text: str) -> list[str]:
    """
    Splits text into paragraphs.
    A paragraph is separated by one or more blank lines.
    """
    paragraphs = re.split(r'\n\s*\n+', text)
    return [p.strip() for p in paragraphs if p.strip()]


def estimate_token_count(text: str) -> int:
    """Estimate the token count for text.

    Uses tiktoken (cl100k_base, the encoding for GPT-4/ada embeddings and a
    reasonable proxy for other modern BPE tokenizers) when available. Falls back
    to a character-based heuristic that is more accurate than a flat word count
    multiplier for German compound words and CJK-style text.
    """
    if not text:
        return 0
    try:
        enc = _TIKTOKEN_ENC
    except NameError:
        pass
    else:
        if enc is not None:
            return len(enc.encode(text))
    # Fallback heuristic: ~4 characters per token on average for mixed
    # English/German text. Whitespace-separated words average ~1.5 tokens for
    # German, but character-based estimation degrades more gracefully for very
    # long compound words and languages without spaces.
    return max(1, len(text) // 4)


def split_sentences_respecting_bounds(text: str) -> List[str]:
    """
    Split text into sentences while respecting legal abbreviations and notation.
    This prevents splitting at abbreviations like 'Abs.', 'Nr.', etc. in legal texts.
    """
    # List of common German legal abbreviations that should not be treated as sentence endings
    abbreviations = [
        'Abs', 'Nr', 'S', 'Ziff', 'Bsp', 'u.a', 'ff', 'z.B', 'Art', 'EG',
        'u.U', 'd.h', 'i.S.v', 'z.T', 'usw', 'etc', 'vgl', 's.o', 's.u',
        'f', 'm', 'o.k', 'g.g.A', 'u.g', 'i.H.v', 'i.V.m', 'z.G', 'sog',
        '§§'  # Multiple paragraph symbols
    ]

    # Create a pattern that matches sentence endings but excludes legal abbreviations
    # This pattern looks for sentence ending punctuation followed by whitespace and capital letter
    # but excludes known abbreviations

    # First, protect abbreviations by replacing them with a placeholder
    protected_text = text
    placeholder_map = {}

    for i, abbr in enumerate(abbreviations):
        placeholder = f"__ABBR_{i}__"
        # Use word boundaries to match abbreviations followed by a period
        pattern = r'\b' + re.escape(abbr) + r'\.'
        protected_text = re.sub(pattern, f"{placeholder}.", protected_text)
        placeholder_map[placeholder] = abbr

    # Now split on sentence boundaries (., !, ?) followed by whitespace and capital letter
    # Also handle cases where sentences end with quotes or parentheses before the next capital
    sentence_pattern = r'[.!?]+[\'"»\])]*\s+(?=[A-ZÄÖÜ])'
    sentences = re.split(sentence_pattern, protected_text)

    # Restore the original abbreviations
    restored_sentences = []
    for sentence in sentences:
        restored_sentence = sentence
        for placeholder, abbr in placeholder_map.items():
            restored_sentence = restored_sentence.replace(f"{placeholder}.", f"{abbr}.")
        restored_sentences.append(restored_sentence.strip())

    # Filter out empty sentences
    return [s for s in restored_sentences if s.strip()]




def _split_words_by_token_budget(
    words: List[str], max_tokens: int = MAX_CHUNK_SIZE
) -> List[str]:
    """Split a list of words into chunks that each respect the token budget.

    Greedily accumulates words into a chunk, re-measuring the FULL proposed
    chunk text each iteration, until adding the next word would exceed
    ``max_tokens``; then starts a new chunk. Re-measuring the whole buffer
    (rather than summing per-word deltas) guarantees the cap holds exactly,
    because BPE tokenizers like tiktoken are not additive — ``encode(" a b")``
    need not equal ``encode(" a") + encode(" b")``. This guarantees no chunk
    exceeds the cap regardless of language or content type (German compound
    words, markdown tables, long URLs, etc.).

    A single word whose own token count exceeds ``max_tokens`` is emitted as
    its own chunk — we cannot split below word granularity without destroying
    content. This is acceptable: a 600-token single token would require ~2400
    characters with no whitespace, which does not occur in natural text.
    """
    if not words:
        return []
    chunks: List[str] = []
    current_words: List[str] = []
    for word in words:
        # Re-encode the full proposed chunk to get the exact token count —
        # do NOT rely on a running sum, which drifts for non-additive BPE.
        candidate = current_words + [word]
        candidate_text = " ".join(candidate)
        if estimate_token_count(candidate_text) > max_tokens and current_words:
            # This word would overflow; flush current buffer and start fresh.
            chunks.append(" ".join(current_words))
            current_words = [word]
        else:
            current_words = candidate
    if current_words:
        chunks.append(" ".join(current_words))
    return chunks


def chunk_document(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Split document into paragraph-level chunks with minimum size enforcement"""
    if not isinstance(doc, dict) or 'text' not in doc:
        logger.log(f"Invalid document format: {doc}", "WARNING")
        return []

    full_text = doc["text"]
    processed_chunks = []

    try:
        # Split text into paragraphs
        paragraphs = split_into_paragraphs(full_text)

        chunk_id = 0
        buffer = []
        buffer_token_count = 0

        for para in paragraphs:
            token_count = estimate_token_count(para)

            if token_count > MAX_CHUNK_SIZE:
                # Handle oversized paragraph by splitting it
                # First, flush current buffer if it has content
                if buffer:
                    processed_chunks.append({
                        "doc_id": doc["doc_id"],
                        "chunk_id": chunk_id,
                        "text": "\n\n".join(buffer),
                        "source": doc["source"],
                        "collection_name": doc.get("collection_name"),
                    })
                    chunk_id += 1
                    buffer = []
                    buffer_token_count = 0

                # Try sentence-aware splitting first for legal texts
                sentences = split_sentences_respecting_bounds(para)

                if len(sentences) > 1:
                    # We have multiple sentences, try to group them respecting MAX_CHUNK_SIZE
                    sentence_buffer = []

                    for sentence in sentences:
                        sentence_token_count = estimate_token_count(sentence)

                        if sentence_token_count > MAX_CHUNK_SIZE:
                            # Individual sentence is too large; split by words
                            # with token-aware budgeting so no chunk exceeds cap.
                            sentence_chunks = _split_words_by_token_budget(
                                sentence.split()
                            )

                            for chunk_text in sentence_chunks:
                                processed_chunks.append({
                                    "doc_id": doc["doc_id"],
                                    "chunk_id": chunk_id,
                                    "text": chunk_text,
                                    "source": doc["source"],
                                    "collection_name": doc.get("collection_name"),
                                })
                                chunk_id += 1
                        else:
                            # Re-encode the full proposed buffer to get the exact
                            # token count. Do NOT sum per-sentence counts — BPE
                            # tokenizers are not additive, so the joined text can
                            # tokenize to more (or fewer) tokens than the sum of
                            # the parts. Mirrors _split_words_by_token_budget and
                            # guarantees the MAX_CHUNK_SIZE cap holds exactly.
                            candidate = sentence_buffer + [sentence]
                            candidate_text = " ".join(candidate)
                            if sentence_buffer and estimate_token_count(candidate_text) > MAX_CHUNK_SIZE:
                                # Emit current buffer as chunk
                                processed_chunks.append({
                                    "doc_id": doc["doc_id"],
                                    "chunk_id": chunk_id,
                                    "text": " ".join(sentence_buffer),
                                    "source": doc["source"],
                                    "collection_name": doc.get("collection_name"),
                                })
                                chunk_id += 1
                                # Start new buffer with current sentence
                                sentence_buffer = [sentence]
                            else:
                                # Add sentence to buffer
                                sentence_buffer.append(sentence)

                    # Emit remaining sentences in buffer if any
                    if sentence_buffer:
                        processed_chunks.append({
                            "doc_id": doc["doc_id"],
                            "chunk_id": chunk_id,
                            "text": " ".join(sentence_buffer),
                            "source": doc["source"],
                            "collection_name": doc.get("collection_name"),
                        })
                        chunk_id += 1
                else:
                    # Fallback to word-based splitting if no sentences were found.
                    # Token-aware so no chunk exceeds cap even for German compounds.
                    para_chunks = _split_words_by_token_budget(para.split())

                    for chunk_text in para_chunks:
                        processed_chunks.append({
                            "doc_id": doc["doc_id"],
                            "chunk_id": chunk_id,
                            "text": chunk_text,
                            "source": doc["source"],
                            "collection_name": doc.get("collection_name"),
                        })
                        chunk_id += 1
            else:
                # Pre-add MAX cap check: re-encode the full proposed buffer to
                # verify the joined text stays under MAX_CHUNK_SIZE. BPE
                # tokenizers are not additive, so '\n\n'.join(buffer + [para])
                # can tokenize to more than the sum of the individual paragraph
                # counts. Mirrors _split_words_by_token_budget and the sentence
                # path above; guarantees the cap holds exactly. When flushing,
                # the emitted buffer was already verified <= MAX when its last
                # paragraph was committed, so it stays within bounds.
                candidate = buffer + [para]
                candidate_text = "\n\n".join(candidate)
                if buffer and estimate_token_count(candidate_text) > MAX_CHUNK_SIZE:
                    processed_chunks.append({
                        "doc_id": doc["doc_id"],
                        "chunk_id": chunk_id,
                        "text": "\n\n".join(buffer),
                        "source": doc["source"],
                        "collection_name": doc.get("collection_name"),
                    })
                    chunk_id += 1
                    buffer = [para]
                    buffer_token_count = token_count
                else:
                    buffer = candidate
                    buffer_token_count += token_count

                # If buffer has reached minimum size, emit as chunk.
                # The rough running sum is fine for the MIN threshold; the exact
                # MAX cap was already enforced by the pre-add check above, and
                # the buffer just committed is <= MAX by construction.
                if buffer_token_count >= MIN_CHUNK_SIZE:
                    processed_chunks.append({
                        "doc_id": doc["doc_id"],
                        "chunk_id": chunk_id,
                        "text": "\n\n".join(buffer),
                        "source": doc["source"],
                        "collection_name": doc.get("collection_name"),
                    })
                    chunk_id += 1
                    buffer = []
                    buffer_token_count = 0

        # Handle remaining content in buffer - emit as final chunk even if smaller than MIN_CHUNK_SIZE
        if buffer:
            processed_chunks.append({
                "doc_id": doc["doc_id"],
                "chunk_id": chunk_id,
                "text": "\n\n".join(buffer),
                "source": doc["source"],
                "collection_name": doc.get("collection_name"),
            })

        return processed_chunks
    except Exception as e:
        logger.log(f"Error chunking document {doc.get('source', 'unknown')}: {e}", "ERROR")
        return []

def get_embedding_function():
    """Get embeddings with retry"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if PROVIDER == "ollama":
                return OllamaEmbeddings(
                    model=EMBEDDING_MODEL_NAME,
                    base_url=OLLAMA_BASE_URL,
                )
            else:  # OpenAI and OpenAI-compatible providers
                return OpenAIEmbeddings(
                    model=EMBEDDING_MODEL_NAME,
                    api_key=OPENAI_API_KEY,
                    base_url=OPENAI_BASE_URL,
                    chunk_size=50,
                )
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            wait_time = (attempt + 1) * 2
            logger.log(f"Embedding connection failed (attempt {attempt + 1}), retrying in {wait_time}s...", "WARNING")
            time.sleep(wait_time)

def save_chunks_to_disk(chunks: List[Dict[str, Any]], path: str = CHUNKS_DIR, collection_name: str = QDRANT_COLLECTION, pending: bool = True):
    """Save chunks to disk with error handling. If pending=True, save to .pending directory for two-phase commit."""
    saved_count = 0
    errors = 0

    # Use collection-specific subfolder
    if pending:
        # Save to .pending directory for two-phase commit
        collection_chunks_dir = os.path.join(path, collection_name, ".pending")
    else:
        # Save directly to main directory (for compatibility with old code)
        collection_chunks_dir = os.path.join(path, collection_name)
    os.makedirs(collection_chunks_dir, exist_ok=True)

    for chunk in chunks:
        if not isinstance(chunk, dict) or 'doc_id' not in chunk or 'chunk_id' not in chunk:
            logger.log(f"Invalid chunk format: {chunk}", "WARNING")
            errors += 1
            continue

        chunk_id = f"{chunk['doc_id']}_{chunk['chunk_id']}"
        chunk_file_path = f"{collection_chunks_dir}/{chunk_id}.json"
        temp_path = f"{chunk_file_path}.tmp"

        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(chunk, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, chunk_file_path)
            saved_count += 1
        except Exception as e:
            logger.log(f"Could not save chunk {chunk_file_path}: {e}", "WARNING")
            errors += 1
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass

    logger.log(f"Saved {saved_count} chunks to {collection_chunks_dir} (errors: {errors})", "INFO")

def move_chunks_from_pending(doc_ids: List[int], path: str = CHUNKS_DIR, collection_name: str = QDRANT_COLLECTION) -> int:
    """Move chunks from .pending directory to main directory after successful embedding.
    
    Args:
        doc_ids: List of document IDs to move chunks for
        path: Base chunks directory
        collection_name: Collection name
    
    Returns:
        Number of chunks moved
    """
    moved_count = 0
    errors = 0
    
    pending_dir = os.path.join(path, collection_name, ".pending")
    main_dir = os.path.join(path, collection_name)
    
    if not os.path.exists(pending_dir):
        logger.log(f"Pending directory does not exist: {pending_dir}", "INFO")
        return 0
    
    os.makedirs(main_dir, exist_ok=True)
    
    for doc_id in doc_ids:
        pattern = f"{doc_id}_*.json"
        for chunk_file in Path(pending_dir).glob(pattern):
            dest = Path(main_dir) / chunk_file.name
            try:
                # Check if destination exists
                if dest.exists():
                    # Destination exists, remove it first
                    dest.unlink()
                # Move the file
                chunk_file.rename(dest)
                moved_count += 1
            except Exception as e:
                logger.log(f"Could not move chunk {chunk_file} to main directory: {e}", "WARNING")
                errors += 1
    
    logger.log(f"Moved {moved_count} chunks from .pending to {collection_name} (errors: {errors})", "INFO")
    return moved_count

def cleanup_pending_chunks(path: str = CHUNKS_DIR, collection_name: str = QDRANT_COLLECTION) -> int:
    """Clean up pending chunks for a collection. Called on startup to handle failed runs.
    
    Args:
        path: Base chunks directory
        collection_name: Collection name (or None to clean all collections)
    
    Returns:
        Number of chunks cleaned up
    """
    cleaned_count = 0
    
    if collection_name:
        collections = [collection_name]
    else:
        # Get all collection directories
        chunks_path = Path(path)
        if not chunks_path.exists():
            return 0
        collections = [d.name for d in chunks_path.iterdir() if d.is_dir() and d.name != ".removed"]
    
    for coll_name in collections:
        pending_dir = os.path.join(path, coll_name, ".pending")
        if os.path.exists(pending_dir):
            for chunk_file in Path(pending_dir).glob("*.json"):
                try:
                    chunk_file.unlink()
                    cleaned_count += 1
                except Exception as e:
                    logger.log(f"Could not remove pending chunk {chunk_file}: {e}", "WARNING")
            
            # Try to remove the .pending directory if empty
            try:
                if not os.listdir(pending_dir):
                    os.rmdir(pending_dir)
            except Exception:
                pass
    
    if cleaned_count > 0:
        logger.log(f"Cleaned up {cleaned_count} pending chunks", "INFO")
    
    return cleaned_count

def load_cached_chunks(path: str = CHUNKS_DIR):
    # Use collection-specific subfolder
    collection_chunks_dir = os.path.join(path, QDRANT_COLLECTION)
    for file in Path(collection_chunks_dir).glob("*.json"):
        # Skip .pending directory
        if ".pending" in str(file):
            continue
        try:
            with open(file, "r", encoding="utf-8") as f:
                yield json.load(f)
        except Exception as e:
            logger.log(f"Could not read chunk file {file}: {e}", "WARNING")
