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
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

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

# Semaphore to limit concurrent embedding requests to prevent server overload
# With larger ubatch-size, we can process more requests safely, but still want some control
embedding_semaphore = threading.Semaphore(4)  # Allow up to 4 concurrent embedding requests
