"""Mocked Gemini pipeline tests — no network, no key required."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gta_autoclipper.write_llm import write_header_vo, grammar_ok, _fit_six_words
from gta_autoclipper.gemini_client import has_gemini
from gta_autoclipper.meta_signals import is_mashup_title
from gta_autoclipper.entities import detect_reactor, event_summary, extract_entities
from gta_autoclipper.scene_vlm import self_review


def test_grammar():
    assert grammar_ok("Tengen vs Gyutaro")
    assert not grammar_ok("Gonna hits different")
    assert not grammar_ok("Blades and.")


def test_fit_six():
    assert len(_fit_six_words("A B C D E F G H").split()) == 6


def test_mashup_and_reactor():
    assert is_mashup_title("GTA 6 Reaction Mashup")
    assert detect_reactor("Kai Cenat Reacts To GTA 6 Trailer", "Kai Cenat Live") == "Kai Cenat"
    assert detect_reactor("Streamers React To Moistcr1tikal", "x") is None


def test_event_vs_from_title():
    es = extract_entities(title="Demon Slayer Tengen vs Gyutaro Fight", niche_id="anime")
    ev = event_summary(es, "Demon Slayer Tengen vs Gyutaro Fight")
    assert "Tengen" in ev and "Gyutaro" in ev
    assert "Tanjiro" not in ev


def test_write_fallback_without_key():
    with patch("gta_autoclipper.gemini_client.resolve_api_key", return_value=""):
        assert not has_gemini({})
        out = write_header_vo(
            niche_id="anime",
            title="Demon Slayer Tengen vs Gyutaro Fight",
            channel="Crunchyroll",
            vlm_desc="Tengen Uzui clashes with Gyutaro with spinning blades.",
            transcript="",
            event="fight",
            cfg={},
        )
    assert out["via"] == "fallback"
    assert "Tengen" in out["header"]


def test_write_with_mocked_gemini():
    fake = "HEADER: Kai Cenat loses it\nVO: Chat went crazy."
    with patch("gta_autoclipper.gemini_client.resolve_api_key", return_value="fake-key"), \
         patch("gta_autoclipper.gemini_client.generate", return_value=fake):
        out = write_header_vo(
            niche_id="gta6",
            title="Kai Cenat Reacts To GTA 6",
            channel="Kai Cenat Live",
            vlm_desc="Kai Cenat looks shocked watching the GTA 6 trailer.",
            transcript="oh my god",
            event="Kai Cenat reacts",
            cfg={},
        )
    assert out["via"] == "gemini"
    assert "Kai" in out["header"]
    assert grammar_ok(out["vo"].rstrip("."))


def test_self_review_mock():
    fake = "DESC: Kai Cenat looks shocked on face cam.\nMATCH: yes\nEMOTION: shocked\nREASON: ok"
    Path("/tmp/x.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    with patch("gta_autoclipper.gemini_client.resolve_api_key", return_value="fake-key"), \
         patch("gta_autoclipper.scene_vlm.grab_frame_files", return_value=[Path("/tmp/x.jpg")]), \
         patch("gta_autoclipper.gemini_client.generate", return_value=fake), \
         patch("gta_autoclipper.scene_vlm._cleanup"):
        rev = self_review(Path("/tmp"), "Kai Cenat reacts", start=0, end=5, cfg={}, title="Kai Cenat Reacts")
    assert rev["ok"] is True
    assert "Kai" in rev["desc"]


if __name__ == "__main__":
    test_grammar()
    test_fit_six()
    test_mashup_and_reactor()
    test_event_vs_from_title()
    test_write_fallback_without_key()
    test_write_with_mocked_gemini()
    test_self_review_mock()
    print("ALL_TESTS_PASSED")
