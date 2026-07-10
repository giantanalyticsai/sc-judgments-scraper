"""CAPTCHA OCR using the pre-trained PARSeq ONNX model.

Ported from the district-court scraper's captcha_solver, but with the torch /
torchvision / onnx dependencies dropped: preprocessing and greedy decoding are
reimplemented in numpy, so only onnxruntime + pillow are needed at runtime.

The model (`captcha.onnx`) is a PARSeq recogniser trained on the securimage
captchas the eCourts / SCR portals use. It emits per-position logits over a
fixed character vocabulary; we take the greedy argmax per position and truncate
at the end-of-sequence token.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

import numpy as np
import onnxruntime as ort
from PIL import Image

from ..config import CAPTCHA_MODEL_PATH
from .model import ensure_model

# --- Vocabulary --------------------------------------------------------------
# This charset literal MUST match the one the model was trained with, character
# for character (indices are meaningful). Reproduced verbatim from the original
# solver, including the raw-string escaping.
_CHARSET = r"0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"

# The trained tokenizer places EOS first, then the charset (with the literal
# characters of "[UNK]" appended as individual tokens), then BOS and PAD. EOS is
# therefore index 0 and marks where a decoded sequence ends.
_ITOS = ("[E]",) + tuple(_CHARSET + "[UNK]") + ("[B]", "[P]")
_EOS_ID = 0

# --- Preprocessing constants (match training transform) ----------------------
_IMG_H, _IMG_W = 32, 128
_NORM_MEAN, _NORM_STD = 0.5, 0.5


class CaptchaSolver:
    """Loads the ONNX model once and solves captcha images on demand."""

    def __init__(self, model_path: Union[str, Path] = CAPTCHA_MODEL_PATH):
        # Fetch + verify the model on first use (it is not shipped in the repo).
        model_path = ensure_model(Path(model_path))
        self._session = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )
        self._input_name = self._session.get_inputs()[0].name

    def _preprocess(self, img: Image.Image) -> np.ndarray:
        """PIL image -> normalized NCHW float32 tensor (batch size 1)."""
        img = img.convert("RGB").resize((_IMG_W, _IMG_H), Image.BICUBIC)
        arr = np.asarray(img, dtype=np.float32) / 255.0  # HWC in [0, 1]
        arr = (arr - _NORM_MEAN) / _NORM_STD
        arr = arr.transpose(2, 0, 1)  # CHW
        return arr[np.newaxis, ...]  # NCHW

    def solve(self, image: Union[str, Path, Image.Image]) -> str:
        """Return the model's predicted text for a captcha image.

        Accepts a PIL image or a path to an image file.
        """
        if not isinstance(image, Image.Image):
            image = Image.open(image)
        x = self._preprocess(image)
        logits = self._session.run(None, {self._input_name: x})[0]  # (1, L, C)
        ids = logits[0].argmax(axis=-1)  # greedy per position

        chars = []
        for token_id in ids:
            if token_id == _EOS_ID:
                break
            chars.append(_ITOS[token_id])
        return "".join(chars).strip()
