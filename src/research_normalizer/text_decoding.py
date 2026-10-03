"""Decode bytes to text when the encoding is unknown.

Research files arrive in many encodings: one sample README is Windows-1252, the data files are
UTF-8 with a BOM. Guessing wrong corrupts every accented name, so we use a deterministic ladder and
record which rung succeeded (DESIGN.md section 10). We measured that encoding *detectors* are
unreliable on short Western-European text (chardet scored a cp1252 file as Windows-1250 at 0.05
confidence), so the detector is only one gated rung, not the whole strategy.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass

import chardet  # Ref [7]

from .config import PipelineConfig

# Byte-order marks, longest first so UTF-32 is tested before UTF-16 (they share a prefix).
_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


@dataclass(frozen=True, slots=True)
class DecodeResult:
    """Decoded text plus how we got there, so provenance can record it."""

    text: str
    encoding: str
    rung: str          # which ladder rung succeeded: bom | utf-8 | detected | cp1252 | latin-1
    confidence: float  # 1.0 for certain rungs, detector score for 'detected', low for fallbacks
    replaced_chars: int  # count of U+FFFD replacements (only possible on the latin-1 rung... it has none)


# Control bytes other than tab/newline/carriage-return/form-feed hardly ever occur in text files;
# above this share of a sample we call the file binary.
_MAX_CONTROL_BYTE_SHARE = 0.05
_TEXT_CONTROL_BYTES = frozenset(b"\t\n\r\f\b")


def looks_like_text(sample: bytes) -> bool:
    """Cheap binary/text test on the first bytes of a file.

    A BOM means text (UTF-16/32 legitimately contain NUL bytes). Otherwise a NUL byte or many
    control characters means binary, the same rule ``file``/git use.
    """
    if not sample:
        return True
    if any(sample.startswith(bom) for bom, _ in _BOMS):
        return True
    if b"\x00" in sample:
        return False
    control = sum(1 for b in sample if b < 32 and b not in _TEXT_CONTROL_BYTES)
    return control / len(sample) <= _MAX_CONTROL_BYTE_SHARE


def _strict_decode(data: bytes, encoding: str) -> str | None:
    """Try a strict decode; return None if it fails, so the ladder can fall through."""
    try:
        return data.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return None


def decode_bytes(data: bytes, config: PipelineConfig | None = None) -> DecodeResult:
    """Decode ``data`` to text using the deterministic ladder in DESIGN.md section 10.

    The ladder, in order:
      1. BOM  -> the encoding the BOM names (certain).
      2. strict UTF-8 (the modern default; certain when it succeeds).
      3. chardet, accepted only at/above ``encoding_min_confidence``.
      4. strict cp1252 (the common Western/Canadian legacy encoding).
      5. latin-1, which decodes any byte sequence (last resort, low confidence).
    """
    config = config or PipelineConfig()

    # Rung 1: BOM.
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            # utf-8-sig / utf-16 / utf-32 strip the BOM themselves on decode.
            text = data.decode(encoding, errors="replace")
            return DecodeResult(text, encoding, "bom", 1.0, text.count("\ufffd"))

    # Rung 2: strict UTF-8 over the whole input.
    if (text := _strict_decode(data, "utf-8")) is not None:
        return DecodeResult(text, "utf-8", "utf-8", 1.0, 0)

    # Rung 3: detector, but only trust a confident verdict on a bounded sample.
    guess = chardet.detect(data[: config.decode_sample_bytes])
    enc, conf = guess.get("encoding"), guess.get("confidence") or 0.0
    if enc and conf >= config.encoding_min_confidence:
        if (text := _strict_decode(data, enc)) is not None:
            return DecodeResult(text, enc.lower(), "detected", float(conf), 0)

    # Rung 4: cp1252 (handles the dairy README's smart quotes and accents).
    if (text := _strict_decode(data, "cp1252")) is not None:
        return DecodeResult(text, "cp1252", "cp1252", 0.6, 0)

    # Rung 5: latin-1 never raises; every byte maps to a code point.
    text = data.decode("latin-1")
    return DecodeResult(text, "latin-1", "latin-1", 0.3, 0)
