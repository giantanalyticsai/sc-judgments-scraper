#!/usr/bin/env python3
"""Pre-fetch the captcha ONNX model.

Handy for Docker builds (`RUN uv run python fetch_model.py`) so the image ships
with the model already cached, and for warming the cache before a first run.
The model is downloaded and hash-verified by src/captcha/model.py.
"""

import logging

from src.captcha.model import ensure_model

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    path = ensure_model()
    print(f"Captcha model ready at {path}")
