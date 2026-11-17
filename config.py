"""
Configuration module for PoliTO Agent.
Loads settings from environment variables and sets defaults.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Base paths
PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

# Credentials
POLITO_USERNAME = os.getenv("POLITO_USERNAME", "")
POLITO_PASSWORD = os.getenv("POLITO_PASSWORD", "")

# Destination folder for organized files
raw_output = os.getenv("POLITO_OUTPUT_DIR", "~/Desktop/PoliTO")
POLITO_OUTPUT_DIR = Path(os.path.expanduser(raw_output)).resolve()

# State, profile, temp, and PDF storage
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

TEMP_DIR = PROJECT_ROOT / "temp"
PDF_OUTPUT_DIR = PROJECT_ROOT / "appunti_pdf"
CHROME_PROFILE_DIR = PROJECT_ROOT / ".chrome_polito_profile"

SESSION_FILE = DATA_DIR / "session.json"
INDEX_FILE = DATA_DIR / "materials_catalog.json"

# URLs
POLITO_BASE_URL = "https://www.polito.it"
POLITO_IDP_LOGIN_URL = "https://idp.polito.it/home"
POLITO_DIDATTICA_URL = "https://didattica.polito.it"

# LLM configuration (OpenAI-compatible primary + Gemini fallback)
API_BASE_URL = os.getenv("API_BASE_URL", "https://openrouter.ai/api/v1").strip()
API_KEY = os.getenv("API_KEY", "").strip()
MODEL_NAME = os.getenv("MODEL_NAME", "google/gemini-2.5-pro").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Local Whisper transcription configuration
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "").strip()
WHISPER_MODEL_EN = os.getenv("WHISPER_MODEL_EN", "distil-medium.en").strip()
WHISPER_MODEL_IT = os.getenv("WHISPER_MODEL_IT", "medium").strip()
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8").strip()

# Shared course aliases for natural language queries and slide matching
COURSE_ALIASES = {
    "sistemi": "operating systems",
    "sistemi operativi": "operating systems",
    "os": "operating systems",
    "so": "operating systems",
    "oop": "object oriented programming",
    "oggetti": "object oriented programming",
    "programmazione a oggetti": "object oriented programming",
    "reti": "reti di calcolatori",
    "segnali": "teoria ed elaborazione dei segnali",
    "tes": "teoria ed elaborazione dei segnali",
    "algoritmi": "algoritmi e strutture dati",
    "asd": "algoritmi e strutture dati",
    "fisica": "elettromagnetismo e teoria dei circuiti",
    "fisica 2": "elettromagnetismo e teoria dei circuiti",
    "fisica ii": "elettromagnetismo e teoria dei circuiti",
    "elettrotecnica": "elettromagnetismo e teoria dei circuiti",
    "circuiti": "elettromagnetismo e teoria dei circuiti",
    "elettromagnetismo": "elettromagnetismo e teoria dei circuiti",
    "controlli": "controlli automatici",
    "iot": "technologies for iot ecosystems",
}
