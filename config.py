# Configuration for English Exam Parser
import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
PROJECT_ROOT = Path(__file__).parent
load_dotenv(PROJECT_ROOT / ".env")

# Data directories
DATA_DIR = PROJECT_ROOT / "data"
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"
DEBUG_DIR = DATA_DIR / "debug"

# API Configuration
API_TYPE = os.getenv("API_TYPE", "openai")  # "openai" or "anthropic"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")  # Set this environment variable
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com")  # API base URL
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4-turbo")  # Default model

# Supported Question Types
SUPPORTED_TYPES = [
    "完形填空",
    "语法填空", 
    "阅读",
    "七选五",
    "选词填空",
    "阅读表达",
    "作文"
]

# Question type mapping to English
TYPE_MAPPING = {
    "完形填空": "cloze",
    "语法填空": "grammar",
    "阅读": "reading",
    "七选五": "seven-choose-five",
    "选词填空": "word-choice",
    "阅读表达": "reading-expression",
    "作文": "en-writing"
}

# Blank component mapping by type
BLANK_COMPONENTS = {
    "cloze": "ClozeBlank",
    "grammar": "Input",
    "reading": None,  # Reading doesn't have blanks
    "seven-choose-five": "Blank",
    "word-choice": "Input2",
    "reading-expression": None,
    "en-writing": None
}

# Validation settings
VALIDATION_RULES = {
    "check_section_overlap": True,
    "check_question_continuity": True,
    "check_blank_continuity": True,
    "check_anchor_uniqueness": True,
    "check_section_coverage": True,
    "min_anchor_length": 10,
    "max_anchor_length": 200
}

# Logging
LOG_LEVEL = "INFO"
LOG_FILE = PROJECT_ROOT / "exam_parser.log"

# Debug output for prompt/response inspection
SHOW_AI_DEBUG = True
MAX_LOGGED_RESPONSE_CHARS = 20000

# Ensure directories exist
for directory in [INPUT_DIR, OUTPUT_DIR, DEBUG_DIR]:
    directory.mkdir(parents=True, exist_ok=True)