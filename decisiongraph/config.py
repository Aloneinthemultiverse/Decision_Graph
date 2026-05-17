import os

# LLM settings
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.anthropic.com")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-6")

# Embedding model
EMBED_MODEL = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")

# Storage
STORAGE_DIR = os.getenv("STORAGE_DIR", "./storage")

# Graph settings
COMMUNITY_MIN_SIZE = 5
ENTITY_RESOLUTION_THRESHOLD = 0.92
BEAM_K = 3

# Chunking
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 100

# Agent
MAX_STEPS = 8
MAX_TOKENS = 6000
DECISION_SIMILARITY_THRESHOLD = 0.5

# Query modes
QUERY_MODE_NORMAL = "normal"      # past decisions only
QUERY_MODE_SESSION = "session"    # company data + decisions
QUERY_MODE_DEEP = "deep"          # full ReAct + all three
DEFAULT_QUERY_MODE = "deep"
