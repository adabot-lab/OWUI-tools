import os
import threading

# Configuration
# folder and file storage
INPUT_DIR = "input"
CHUNKS_DIR = "chunks"
METADATA_FILE = "metadata/file_metadata.json"

# Provider
PROVIDER = os.getenv("PROVIDER", "openai").lower()  # "openai" or "ollama"

# Embedding
EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME", "CPP_snowflake-embed-l-v2.0-GGUF")
VECTOR_SIZE = int(os.getenv("VECTOR_SIZE", 1024))  # Embedding dimension

# OpenAI
# Ingestion uses OPENAI_BASE_URL ONLY. BASE_URL is the fast ingestion host; it
# may also serve the main LLM, and batch unloading of that LLM during ingestion
# is acceptable. Retrieval (RAG/02) uses OPENAI_RETRIEVAL_URL to avoid this.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")

# Ollama
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")

# Deliberately re-declared in RAG/02_My_Little_RAG/retrieval_engine.py (self-contained
# subtools, no shared config module). When changing a default here, change the mirror
# there and keep both aligned. The OPENAI_BASE_URL/OPENAI_RETRIEVAL_URL split is
# intentional divergence — see the OpenAI comment above.

# Qdrant
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "rag_chunks")
QDRANT_HOST = os.getenv("QDRANT_HOST", "qdrant")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", 6333))

# Chunking
MAX_CHUNK_SIZE = int(os.getenv("MAX_CHUNK_SIZE", 600))  # Maximum tokens per chunk
MIN_CHUNK_SIZE = int(os.getenv("MIN_CHUNK_SIZE", 200))  # Minimum tokens per chunk
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", 100))    # Reserved: overlap between chunks (not yet implemented in chunking logic)

# BM25 configuration
BM25_LANGUAGE = os.getenv("BM25_LANGUAGE", "german")

# --- Deletion durability & source-readiness (P1 correctness fixes) ---
# Durable pending-deletion ledger. Lives in the metadata volume, OUTSIDE input.
PENDING_DELETIONS_FILE = os.getenv(
    "PENDING_DELETIONS_FILE", "metadata/pending_deletions.json"
)

# Scanner exclusions: repo internals and administrative documents are never
# ingested. Pre-existing metadata entries for excluded paths are carried
# forward unchanged (never silently purged) — see metadata.py.
EXCLUDED_DIR_NAMES = tuple(
    d for d in os.getenv("EXCLUDED_DIR_NAMES", ".git").split(",") if d
)
EXCLUDED_FILENAMES = tuple(
    f for f in os.getenv("EXCLUDED_FILENAMES", "README.md").split(",") if f
)

# Source-readiness guard (checked in main() BEFORE any scan/deletion diff).
# Comma-separated paths RELATIVE TO INPUT_DIR that must exist and be non-empty
# (the expected checkouts / source roots). Empty = no expectation.
EXPECTED_SOURCES = [
    s for s in os.getenv("EXPECTED_SOURCES", "").split(",") if s.strip()
]
# Marker file (relative to INPUT_DIR) that aborts the run when present.
MIGRATION_MARKER = os.getenv("MIGRATION_MARKER", ".migration_in_progress")

# Mass-removal guard: a run whose deleted-file count is >= max(
# MASS_REMOVAL_THRESHOLD * stored_files, MASS_REMOVAL_MIN_COUNT) stops with a
# preview instead of treating the disappearance as intentional deletion.
# Intentional large deletion requires a scoped reviewed manifest:
# MASS_REMOVAL_MANIFEST points at a newline-separated file of path prefixes
# (relative to INPUT_DIR, one per line) that are explicitly allowed to go.
MASS_REMOVAL_THRESHOLD = float(os.getenv("MASS_REMOVAL_THRESHOLD", "0.2"))
MASS_REMOVAL_MIN_COUNT = int(os.getenv("MASS_REMOVAL_MIN_COUNT", "20"))
MASS_REMOVAL_MANIFEST = os.getenv("MASS_REMOVAL_MANIFEST", "")


def validate_required_env():
    required = ["QDRANT_HOST", "QDRANT_COLLECTION", "EMBEDDING_MODEL_NAME", "PROVIDER"]
    provider = os.environ.get("PROVIDER", "").lower()
    if provider == "openai":
        required.append("OPENAI_BASE_URL")
    elif provider == "ollama":
        required.append("OLLAMA_BASE_URL")
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        raise SystemExit(
            f"[RAG/01 ingestion] Missing required environment variables: {', '.join(missing)}\n"
            "Copy RAG/.env.example to the compose project .env and set the listed variables."
        )


# Semaphore to limit concurrent embedding requests to prevent server overload
# With larger ubatch-size, we can process more requests safely, but still want some control
embedding_semaphore = threading.Semaphore(4)  # Allow up to 4 concurrent embedding requests
