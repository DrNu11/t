#!/usr/bin/env python3
"""
Trident Agent MVP — Python Production Engine (thin entry point)

Implementation lives in the `engine/` package (main / ingest / ai_worker /
forward / webhook / alerts / prices / ws_client / utils).

Usage:
  python src_python/engine.py
"""

import asyncio
import sys
from pathlib import Path

# 兼容嵌入式 Python：把本脚本所在目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine.main import main

if __name__ == "__main__":
    asyncio.run(main())
