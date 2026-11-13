"""Regression tests: __ABBR_*__ placeholder artifacts (F2) + sentence-coherent
chunk boundaries (F3).

Run in isolation (never against production):
    cd RAG/01_My_Little_RAG_Ingestion
    python3 -m pytest test_chunking_quality.py -v

Pure unit tests: no network, no Qdrant, no embeddings.
"""
import sys
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVICE_DIR))

import chunking  # noqa: E402

ABBRS = chunking.LEGAL_ABBREVIATIONS


def assert_clean(sentences):
    for s in sentences:
        assert "__ABBR" not in s, f"placeholder leaked: {s!r}"
        assert "\x00" not in s, f"sentinel leaked: {s!r}"


# ---------------------------------------------------------------------------
# F2: no placeholder leakage, for every abbreviation, in every position
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("abbr", ABBRS)
def test_no_leak_sentence_initial(abbr):
    text = f"{abbr}. 5 der Anlage gilt. Weiteres folgt."
    assert_clean(chunking.split_sentences_respecting_bounds(text))


@pytest.mark.parametrize("abbr", ABBRS)
def test_no_leak_mid_sentence(abbr):
    text = (
        f"Die Voraussetzungen nach {abbr}. 2 der Vorschrift liegen vor. "
        "Die Klage ist abgewiesen."
    )
    assert_clean(chunking.split_sentences_respecting_bounds(text))


@pytest.mark.parametrize("abbr", ABBRS)
def test_no_leak_sentence_final(abbr):
    # The observed artifact shape: abbreviation at the very end of a
    # sentence -- the old splitter consumed its period and failed to
    # restore the placeholder ("vgl. S. 5 ff. Weitere ..." -> __ABBR_6__).
    text = f"Siehe die Fundstelle {abbr}. 119. Die Klage ist abgewiesen."
    out = chunking.split_sentences_respecting_bounds(text)
    assert_clean(out)
    joined = " ".join(out)
    assert f"{abbr}." in joined, f"abbreviation lost: {out!r}"


@pytest.mark.parametrize("abbr", ABBRS)
def test_no_leak_only_abbreviation(abbr):
    assert_clean(chunking.split_sentences_respecting_bounds(f"{abbr}. 119"))


def test_no_placeholder_in_longest_first_collision():
    """'S' must not shadow longer forms containing it ('i.S.v', 'g.g.A')."""
    text = "Auslegung nach Art. 4 Abs. 1 i.S.v. Art. 24 GG. Die Norm gilt weiterhin."
    out = chunking.split_sentences_respecting_bounds(text)
    assert_clean(out)
    joined = " ".join(out)
    assert "i.S.v." in joined, joined


def test_realworld_german_legal_passage_clean():
    """F2 end-to-end: the exact artifact shapes observed in the live collection
    (research note 261113, F2) must not reappear."""
    passage = (
        "Gefordert wird ein konkretes und ernsthaftes Interesse. "
        "§ 132 Abs. 2 Satz 1 GWB i.V.m. § 101 GWB haelt dem nicht stand. "
        "Die Kosten des Verfahrens traegt die Antragsgegnerin "
        "(BT-Drs. 18/6281, S. 119). Die Kammer weist das Begehren zurueck."
    )
    out = chunking.split_sentences_respecting_bounds(passage)
    assert_clean(out)
    joined = " ".join(out)
    assert "S. 119" in joined
    assert "§ 132 Abs. 2 Satz 1 GWB" in joined


def test_splitter_preserves_all_text():
    """Re-joining the returned sentences with ' ' reproduces the input modulo
    whitespace -- the splitter must not consume or drop characters."""
    passage = (
        "Die Kosten des Verfahrens traegt die Antragsgegnerin "
        "(BT-Drs. 18/6281, S. 119). Die Klage ist abgewiesen. "
        "Dies ergibt sich aus Nr. 3 der Anlage."
    )
    out = chunking.split_sentences_respecting_bounds(passage)
    rejoined = " ".join(s.strip() for s in out)

    def norm(t):
        return " ".join(t.split())

    assert norm(rejoined) == norm(passage), (rejoined, passage)


# ---------------------------------------------------------------------------
# F3: chunk boundaries respect sentence structure
# ---------------------------------------------------------------------------

def _long_german_paragraph(n_sentences=40):
    """Build an oversized paragraph (> chunk token cap) with distinct sentences."""
    tmpl = ("Die Voraussetzungen des Anspruchs nach § {i} Abs. 2 Satz 1 GWB liegen "
            "nicht vor, weil das Verfahren bereits abgeschlossen war.")
    return " ".join(tmpl.format(i=i) for i in range(1, n_sentences + 1))


def test_pack_sentences_no_mid_sentence_chunk_ends():
    sentences = chunking.split_sentences_respecting_bounds(
        _long_german_paragraph()
    )
    assert len(sentences) > 1
    chunks = chunking.pack_sentences_by_token_budget(sentences, max_tokens=50)
    assert len(chunks) > 1
    for c in chunks:
        # Every chunk must end with terminal punctuation (sentence boundary).
        assert c.rstrip().endswith((".", "!", "?")), f"mid-sentence cut: {c[-40:]!r}"


def test_pack_sentences_respects_token_cap():
    sentences = chunking.split_sentences_respecting_bounds(
        _long_german_paragraph()
    )
    chunks = chunking.pack_sentences_by_token_budget(sentences, max_tokens=120)
    for c in chunks:
        assert chunking.estimate_token_count(c) <= 120


def test_pack_sentences_no_text_loss():
    sentences = chunking.split_sentences_respecting_bounds(
        _long_german_paragraph(20)
    )
    chunks = chunking.pack_sentences_by_token_budget(sentences, max_tokens=80)
    rejoined = " ".join(c.strip() for c in chunks)
    original = " ".join(sentences)
    assert " ".join(rejoined.split()) == " ".join(original.split())


def test_pack_sentences_oversized_sentence_word_split():
    """A single sentence longer than the cap is word-split (unbreakable unit)."""
    huge = "Wort " * 200 + "Ende."
    sentences = chunking.split_sentences_respecting_bounds(huge)
    chunks = chunking.pack_sentences_by_token_budget(sentences, max_tokens=50)
    assert len(chunks) > 1
    assert " ".join(chunks).split() == huge.split()


def test_chunk_document_no_mid_sentence_ends_oversized_paragraph():
    """chunk_document on an oversized paragraph: no chunk ends mid-sentence."""
    doc = {
        "doc_id": 1,
        "text": _long_german_paragraph(30),
        "source": "test/decision.md",
        "collection_name": "test",
    }
    chunks = chunking.chunk_document(doc)
    assert len(chunks) > 1
    for c in chunks:
        text = c["text"].rstrip()
        assert text.endswith((".", "!", "?")), f"mid-sentence cut: {text[-40:]!r}"
        assert "__ABBR" not in text


def test_chunk_document_realworld_legal_passage_clean():
    """F2+F3 end-to-end through chunk_document with observed artifact shapes."""
    doc = {
        "doc_id": 7,
        "text": (
            "Gefordert wird ein konkretes und ernsthaftes Interesse. "
            "§ 132 Abs. 2 Satz 1 GWB i.V.m. § 101 GWB haelt dem nicht stand. "
            "Die Kosten des Verfahrens traegt die Antragsgegnerin "
            "(BT-Drs. 18/6281, S. 119). "
        ) * 12,  # force multiple oversized paragraphs
        "source": "test/bt.md",
        "collection_name": "test",
    }
    chunks = chunking.chunk_document(doc)
    assert chunks
    for c in chunks:
        t = c["text"]
        assert "__ABBR" not in t, f"artifact: {t[:120]!r}"
        assert "\x00" not in t
        assert t.rstrip().endswith((".", "!", "?")), f"mid-sentence: {t[-40:]!r}"


# ---------------------------------------------------------------------------
# Behavior preserved: abbreviation protection still prevents false splits
# ---------------------------------------------------------------------------

def test_abbreviation_not_split_as_sentence_end():
    text = "Siehe § 3 Abs. 2 der Regelung. Die Regelung gilt fort."
    out = chunking.split_sentences_respecting_bounds(text)
    assert out == ["Siehe § 3 Abs. 2 der Regelung.", "Die Regelung gilt fort."]


def test_actual_sentence_boundary_still_split():
    text = "Die Klage ist abgewiesen. Die Kosten tragen die Beteiligten."
    out = chunking.split_sentences_respecting_bounds(text)
    assert len(out) == 2
