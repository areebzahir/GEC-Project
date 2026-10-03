"""Decoding ladder: each rung is chosen for the right input (DESIGN.md section 10)."""

from __future__ import annotations

from research_normalizer.text_decoding import decode_bytes


def test_strict_utf8():
    r = decode_bytes("plain ascii and café".encode("utf-8"))
    assert r.rung == "utf-8" and r.encoding == "utf-8" and r.confidence == 1.0


def test_utf8_bom_stripped():
    # Raw BOM bytes followed by text (how a real BOM-prefixed file looks on disk).
    r = decode_bytes(b"\xef\xbb\xbfsite,temp\nA,1\n")
    assert r.rung == "bom" and r.encoding == "utf-8-sig"
    assert not r.text.startswith("\ufeff")  # BOM removed by the utf-8-sig codec


def test_utf16_bom():
    r = decode_bytes("a,b\n1,é\n".encode("utf-16"))
    assert r.rung == "bom" and r.encoding == "utf-16" and "é" in r.text


def test_cp1252_fallback():
    # Smart quotes / accents that are invalid UTF-8 fall to the cp1252 rung.
    r = decode_bytes("Renée café résumé".encode("cp1252"))
    assert r.rung == "cp1252" and "Renée" in r.text


def test_latin1_never_fails():
    # 0x80-0xFF bytes that are not valid cp1252 control points still decode via latin-1.
    r = decode_bytes(bytes(range(128, 256)))
    assert r.text  # produced something, did not raise
    assert r.rung in {"cp1252", "latin-1"}
