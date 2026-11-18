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


class _TokenBudgetAccumulator:
    """Greedy unit accumulator enforcing an exact MAX_CHUNK_SIZE cap.

    Per-unit token counts are computed once (estimate_token_count per unit).
    A cheap running sum (exact per-unit counts + 1 join allowance per unit, for
    the ' ' or '\\n\\n' separators) gates appends while it stays <= JOIN_GATE_RATIO
    (80%) of max_tokens; once past the gate, every append is verified with an
    exact full-buffer re-encode (non-additive BPE safety). The final text is
    always exact-checked before emit.
    """
    JOIN_GATE_RATIO = 0.8

    def __init__(self, max_tokens: int, joiner: str):
        self._max_tokens = max_tokens
        self._joiner = joiner
        self._units: List[str] = []
        self._cheap_sum = 0  # sum of exact per-unit counts + 1 token per unit for separator
        self._gate_crossed = False

    def add(self, unit: str, unit_token_count: int) -> bool:
        """Try to append a unit. Returns True if appended, False if it would
        exceed max_tokens (caller flushes and re-adds the unit to a fresh
        accumulator). MUST NOT mutate state on a False return."""
        if not self._units:
            # First unit — always accept (unbreakable-unit case handled by caller).
            self._units.append(unit)
            self._cheap_sum = unit_token_count + 1  # +1 for future separator
            if self._cheap_sum > self._max_tokens * self.JOIN_GATE_RATIO:
                self._gate_crossed = True
            return True

        if not self._gate_crossed:
            # Below the gate: accept on the cheap sum WITHOUT exact re-encode.
            # Rationale: join slack cannot realistically add 20% — per-unit
            # counts already include each unit's own tokens; the +1/unit
            # allowance covers separator tokens; below 80% even pathological
            # merges stay under the cap.
            projected = self._cheap_sum + unit_token_count + 1
            if projected <= self._max_tokens * self.JOIN_GATE_RATIO:
                self._units.append(unit)
                self._cheap_sum = projected
                return True
            # Would cross the gate — fall through to exact mode to decide.
            self._gate_crossed = True

        # Above the gate: use exact full-buffer re-encode (worst case
        # degrades to today's quadratic, never worse).
        candidate_text = self._joiner.join(self._units + [unit])
        if estimate_token_count(candidate_text) <= self._max_tokens:
            self._units.append(unit)
            self._cheap_sum += unit_token_count + 1
            return True
        return False

    def text(self) -> str:
        """Return the joined text of accumulated units."""
        return self._joiner.join(self._units)

    def token_count(self) -> int:
        """Cheap sum (with join allowances), NOT exact."""
        return self._cheap_sum

    def unit_token_sum(self) -> int:
        """Plain sum of per-unit token counts WITHOUT join allowances.

        Matches the pre-accumulator buffer_token_count semantics so the
        MIN_CHUNK_SIZE flush fires at exactly the same boundaries as before.
        """
        return self._cheap_sum - len(self._units)

    def exact_token_count(self) -> int:
        """Exact token count via estimate_token_count(self.text())."""
        return estimate_token_count(self.text())


# German legal abbreviations whose period must never be treated as a sentence
# ending (corpus is German-only). Order is irrelevant: the matcher alternation
# is sorted longest-first at module load so longer forms always win
# (e.g. 'i.S.v.' is matched whole, never as 'i' + stray 'S.v.').
LEGAL_ABBREVIATIONS = [
    'Abs', 'Nr', 'S', 'Ziff', 'Bsp', 'u.a', 'ff', 'z.B', 'Art', 'EG',
    'u.U', 'd.h', 'i.S.v', 'z.T', 'usw', 'etc', 'vgl', 's.o', 's.u',
    'f', 'm', 'o.k', 'g.g.A', 'u.g', 'i.H.v', 'i.V.m', 'z.G', 'sog',
    '§§'  # Multiple paragraph symbols
]

_ABBR_SORTED = sorted(LEGAL_ABBREVIATIONS, key=len, reverse=True)

# Matches one protected abbreviation INCLUDING its trailing period.
# Built once at import; used by _protect_abbreviation_periods().
_ABBR_DOT_RE = re.compile(
    r'\b(?:' + '|'.join(re.escape(a) for a in _ABBR_SORTED) + r')\.'
)

# Sentinel that replaces ONLY the final period of a protected abbreviation.
# A control char is used deliberately: it cannot be produced by any real
# corpus text, cannot match the sentence-boundary regex, and is stripped
# again before any sentence is returned — so no marker can leak into chunks.
_ABBR_DOT_SENTINEL = '\x00'

# Splits BETWEEN sentences without consuming anything: the lookbehind keeps
# the terminal punctuation (., !, ?) in the preceding sentence, so re-joining
# sentences with ' ' reproduces the original text (modulo per-part whitespace).
_SENTENCE_BOUNDARY_RE = re.compile(r'(?<=[.!?])(?=[\'"»\])]*\s)')


def _protect_abbreviation_periods(text: str) -> str:
    """Mask the final period of every known legal abbreviation.

    Replaces exactly the '.' of e.g. 'Abs.' / 'S.' / 'i.V.m.' with the
    sentinel character so the sentence splitter cannot treat it as a
    sentence boundary. Single pass, no placeholder tokens — the original
    period is restored by _restore_abbreviation_periods().
    """
    return _ABBR_DOT_RE.sub(
        lambda m: m.group(0)[:-1] + _ABBR_DOT_SENTINEL, text
    )


def _restore_abbreviation_periods(text: str) -> str:
    """Undo _protect_abbreviation_periods(). Lossless by construction."""
    return text.replace(_ABBR_DOT_SENTINEL, '.')


def split_sentences_respecting_bounds(text: str) -> List[str]:
    """Split text into sentences while respecting legal abbreviations and notation.

    This prevents splitting at abbreviations like 'Abs.', 'Nr.', etc. in legal texts.

    Mechanism (single pass, no placeholder round-trip): the final period of
    every abbreviation in LEGAL_ABBREVIATIONS is masked with a control
    character the sentence-boundary regex cannot match, the text is split at
    sentence boundaries WITHOUT consuming the punctuation (lookbehind-based),
    and the masked periods are restored. Because no placeholder token ever
    exists in the returned sentences and the splitter never consumes
    characters, the round-trip is lossless: no '__ABBR_*__' artifact can be
    produced under any input, and sentence-final punctuation is preserved
    (e.g. '(BT-Drs. 18/6281, S. 119).' stays intact at a sentence end).
    """
    protected = _protect_abbreviation_periods(text)
    parts = _SENTENCE_BOUNDARY_RE.split(protected)
    sentences = [_restore_abbreviation_periods(p).strip() for p in parts]
    return [s for s in sentences if s]


def pack_sentences_by_token_budget(
    sentences: List[str], max_tokens: int = MAX_CHUNK_SIZE
) -> List[str]:
    """Pack whole sentences into chunks within the token budget.

    Greedy: appends whole sentences while they fit, starting a new chunk at
    the exact sentence boundary otherwise. Guarantees that every returned
    chunk ends at a sentence boundary (no mid-sentence cuts) — the only
    exception is a single sentence whose own token count exceeds
    ``max_tokens``, which is word-split as a last resort (an unbreakable
    unit; splitting it mid-sentence is unavoidable without dropping text).
    """
    if not sentences:
        return []
    chunks: List[str] = []
    sentence_counts = [estimate_token_count(s) for s in sentences]
    acc = _TokenBudgetAccumulator(max_tokens, " ")
    for i, sentence in enumerate(sentences):
        stc = sentence_counts[i]
        if stc > max_tokens:
            # Unbreakable sentence: flush pending, then word-split it.
            if acc.token_count() > 0:
                chunks.append(acc.text())
                acc = _TokenBudgetAccumulator(max_tokens, " ")
            chunks.extend(_split_words_by_token_budget(sentence.split(), max_tokens))
            continue
        if not acc.add(sentence, stc):
            chunks.append(acc.text())
            acc = _TokenBudgetAccumulator(max_tokens, " ")
            acc.add(sentence, stc)
    if acc.token_count() > 0:
        chunks.append(acc.text())
    return chunks




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
    # Cache per-word token counts so estimate_token_count is called exactly
    # once per word.
    word_counts = [estimate_token_count(w) for w in words]
    acc = _TokenBudgetAccumulator(max_tokens, " ")
    for i, word in enumerate(words):
        if not acc.add(word, word_counts[i]):
            # Flush current buffer, then add rejected word to a fresh accumulator.
            chunk_text = acc.text()
            # Defensive final check: if the exact check somehow exceeds max
            # (cannot happen below the gate by design, but guard defensively),
            # split the buffer text with _split_words_by_token_budget as a
            # last-resort fallback so no emitted chunk violates the cap.
            # A single-unit buffer is the unbreakable-word case — emit as-is
            # (documented above); recursing on it would not terminate.
            if estimate_token_count(chunk_text) > max_tokens:
                sub_words = chunk_text.split()
                if len(sub_words) <= 1:
                    chunks.append(chunk_text)
                else:
                    chunks.extend(_split_words_by_token_budget(sub_words, max_tokens))
            else:
                chunks.append(chunk_text)
            acc = _TokenBudgetAccumulator(max_tokens, " ")
            # Single unit exceeding max (unbreakable word) — accept alone.
            acc.add(word, word_counts[i])
    # Flush remaining words.
    if acc.token_count() > 0:
        chunk_text = acc.text()
        if estimate_token_count(chunk_text) > max_tokens:
            # A single unit whose own count exceeds max_tokens is emitted as-is
            # (unbreakable word, documented above) — recursing on it would not
            # terminate, so only re-split genuinely multi-word buffers.
            sub_words = chunk_text.split()
            if len(sub_words) <= 1:
                chunks.append(chunk_text)
            else:
                chunks.extend(_split_words_by_token_budget(sub_words, max_tokens))
        else:
            chunks.append(chunk_text)
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
        para_acc = _TokenBudgetAccumulator(MAX_CHUNK_SIZE, "\n\n")

        for para in paragraphs:
            token_count = estimate_token_count(para)

            if token_count > MAX_CHUNK_SIZE:
                # Handle oversized paragraph by splitting it.
                # First, flush current accumulator if it has content.
                if para_acc.token_count() > 0:
                    chunk_text = para_acc.text()
                    if estimate_token_count(chunk_text) > MAX_CHUNK_SIZE:
                        # Defensive: buffer text can only exceed the cap if
                        # the accumulator state was inconsistent; pack it by
                        # sentence so the flush still ends at a boundary.
                        for fb in pack_sentences_by_token_budget(
                            split_sentences_respecting_bounds(chunk_text)
                        ):
                            processed_chunks.append({
                                "doc_id": doc["doc_id"],
                                "chunk_id": chunk_id,
                                "text": fb,
                                "source": doc["source"],
                                "collection_name": doc.get("collection_name"),
                            })
                            chunk_id += 1
                    else:
                        processed_chunks.append({
                            "doc_id": doc["doc_id"],
                            "chunk_id": chunk_id,
                            "text": chunk_text,
                            "source": doc["source"],
                            "collection_name": doc.get("collection_name"),
                        })
                        chunk_id += 1
                    para_acc = _TokenBudgetAccumulator(MAX_CHUNK_SIZE, "\n\n")

                # Sentence-aware splitting (F3): pack whole sentences up to
                # MAX_CHUNK_SIZE so no chunk ends mid-sentence. pack_sentences_
                # by_token_budget word-splits only a single sentence that is
                # itself longer than the cap (unbreakable unit).
                sentences = split_sentences_respecting_bounds(para)

                if len(sentences) > 1:
                    for chunk_text in pack_sentences_by_token_budget(sentences):
                        processed_chunks.append({
                            "doc_id": doc["doc_id"],
                            "chunk_id": chunk_id,
                            "text": chunk_text,
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
                # Accumulate paragraph using the shared token-budget accumulator.
                # The accumulator handles exact cap enforcement; below the gate
                # it uses a cheap sum, above it re-encodes the full buffer.
                if not para_acc.add(para, token_count):
                    # Flush current accumulator, then add rejected paragraph.
                    chunk_text = para_acc.text()
                    if estimate_token_count(chunk_text) > MAX_CHUNK_SIZE:
                        # Defensive: pack by sentence so the flush ends at a
                        # sentence boundary (F3), never mid-sentence.
                        for fb in pack_sentences_by_token_budget(
                            split_sentences_respecting_bounds(chunk_text)
                        ):
                            processed_chunks.append({
                                "doc_id": doc["doc_id"],
                                "chunk_id": chunk_id,
                                "text": fb,
                                "source": doc["source"],
                                "collection_name": doc.get("collection_name"),
                            })
                            chunk_id += 1
                    else:
                        processed_chunks.append({
                            "doc_id": doc["doc_id"],
                            "chunk_id": chunk_id,
                            "text": chunk_text,
                            "source": doc["source"],
                            "collection_name": doc.get("collection_name"),
                        })
                        chunk_id += 1
                    para_acc = _TokenBudgetAccumulator(MAX_CHUNK_SIZE, "\n\n")
                    # Single paragraph exceeding max would have been caught
                    # above; this is just a normal add.
                    para_acc.add(para, token_count)

                # If buffer has reached minimum size, emit as chunk.
                # unit_token_sum() (no join allowances) matches the old
                # buffer_token_count semantics exactly; the exact MAX cap was
                # already enforced by the accumulator.
                if para_acc.unit_token_sum() >= MIN_CHUNK_SIZE:
                    chunk_text = para_acc.text()
                    if estimate_token_count(chunk_text) > MAX_CHUNK_SIZE:
                        # Defensive: pack by sentence so the flush ends at a
                        # sentence boundary (F3), never mid-sentence.
                        for fb in pack_sentences_by_token_budget(
                            split_sentences_respecting_bounds(chunk_text)
                        ):
                            processed_chunks.append({
                                "doc_id": doc["doc_id"],
                                "chunk_id": chunk_id,
                                "text": fb,
                                "source": doc["source"],
                                "collection_name": doc.get("collection_name"),
                            })
                            chunk_id += 1
                    else:
                        processed_chunks.append({
                            "doc_id": doc["doc_id"],
                            "chunk_id": chunk_id,
                            "text": chunk_text,
                            "source": doc["source"],
                            "collection_name": doc.get("collection_name"),
                        })
                        chunk_id += 1
                    para_acc = _TokenBudgetAccumulator(MAX_CHUNK_SIZE, "\n\n")

        # Handle remaining content in accumulator - emit as final chunk even if smaller than MIN_CHUNK_SIZE
        if para_acc.token_count() > 0:
            chunk_text = para_acc.text()
            if estimate_token_count(chunk_text) > MAX_CHUNK_SIZE:
                # Defensive: pack by sentence so the final flush ends at a
                # sentence boundary (F3), never mid-sentence.
                for fb in pack_sentences_by_token_budget(
                    split_sentences_respecting_bounds(chunk_text)
                ):
                    processed_chunks.append({
                        "doc_id": doc["doc_id"],
                        "chunk_id": chunk_id,
                        "text": fb,
                        "source": doc["source"],
                        "collection_name": doc.get("collection_name"),
                    })
                    chunk_id += 1
            else:
                processed_chunks.append({
                    "doc_id": doc["doc_id"],
                    "chunk_id": chunk_id,
                    "text": chunk_text,
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
