#!/usr/bin/env python3
"""
Dual File and Console Logging Engine for Auto DL.
"""

import logging
import sys
from bot_config import LOG_FILE

# Clear log file on startup as requested
try:
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write("=== PrimeX Auto DL Engine Initialized ===\n")
except Exception:
    pass

logger = logging.getLogger("AutoDL")
logger.setLevel(logging.INFO)

# Console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter(
    "%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
console_handler.setFormatter(console_formatter)

# File handler
file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setLevel(logging.INFO)
file_formatter = logging.Formatter(
    "%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
file_handler.setFormatter(file_formatter)

if not logger.handlers:
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
