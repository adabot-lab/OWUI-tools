"""LLM-based extraction of legal paragraphs from raw markdown chunks."""
import json
import re
import os
import httpx

# The system prompt instructs the LLM to extract structured paragraphs.
# Critical: emphasize JSON-only output, preserving § or Artikel prefix in content,
# removing footnotes, and not inventing content.
EXTRACTION_SYSTEM_PROMPT = """\
Du bist ein juristischer Datenextraktor. Deine Aufgabe ist es, aus einem rohen Markdown-Text \
eines deutschen Gesetzes alle Paragraphen (§) oder Artikel strukturiert als JSON zu extrahieren.

Regeln:
1. Extrahiere JEDES Vorkommen von § NNN oder Artikel NNN als separaten Eintrag.
2. section_number: Nur die Nummer (z.B. "1", "127a", "10b").
3. section_type: "paragraph" für §, "article" für Artikel.
4. title: Der Titel des Paragraphen falls vorhanden (z.B. "Gegenstand und Anwendungsbereich"), \
sonst leerer String. Steht typischerweise direkt nach der Nummer.
5. content: Der VOLLSTÄNDIGE Text des Paragraphen inklusive der §/Artikel-Nummer am Anfang. \
Behalte alle Absätze (1), (2), etc. und Nummerierungen bei.
6. ENTFERNE alle Fußnoten (Zeilen die mit "Fußnote" beginnen oder "(+++ § ...)" Pattern).
7. ENTFERNE Seitenzahlen, Header/Footer Artefakte.
8. ENTFERNE markdown Überschriften (##, ###) aus dem content — diese sind Strukturmarker, kein Gesetzestext.
9. ÄNDERE NICHT den eigentlichen Gesetzestext. Keine Zusammenfassungen, keine Paraphrasierungen.
10. Erfinde KEINE Paragraphen, die nicht im Text stehen.

Antworte AUSSCHLIESSLICH mit gültigem JSON in diesem Format:
{"paragraphs": [{"section_number": "1", "section_type": "paragraph", "title": "...", "content": "..."}]}\
"""

EXTRACTION_USER_TEMPLATE = """\
Extrahiere alle Paragraphen/Artikel aus folgendem Gesetzestext als JSON:

---BEGIN TEXT---
{chunk_text}
---END TEXT---

Gib nur das JSON zurück, keine Erklärungen.\
"""


class LLMExtractor:
    """Calls a local LLM to extract structured legal paragraphs from markdown."""

    def __init__(self, base_url: str = None, model: str = None, api_key: str = "dummy"):
        self.base_url = base_url or os.getenv("LLM_BASE_URL", "http://localhost:4000/v1")
        self.model = model or os.getenv("LLM_EXTRACT_MODEL", "zai-glm-4.7")
        self.api_key = api_key
        self.client = httpx.Client(timeout=180.0)

    def extract(self, chunk_text: str) -> list[dict]:
        """Send a markdown chunk to the LLM and return parsed paragraphs.

        Returns:
            List of dicts with keys: section_number, section_type, title, content.
            Returns empty list on any error.
        """
        try:
            response = self.client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                        {"role": "user",
                         "content": EXTRACTION_USER_TEMPLATE.format(chunk_text=chunk_text)},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 12000,
                },
            )
        except httpx.RequestError as e:
            print(f"LLM request failed: {e}")
            return []

        if response.status_code != 200:
            print(f"LLM returned status {response.status_code}: {response.text[:200]}")
            return []

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as e:
            print(f"Failed to parse LLM response: {e}")
            return []

        # Strip markdown code fences if present
        content = self._strip_code_fences(content)

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as e:
            print(f"LLM returned invalid JSON: {e}")
            print(f"Content preview: {content[:200]}")
            return []

        paragraphs = parsed.get("paragraphs", [])
        if not isinstance(paragraphs, list):
            return []

        # Validate and deduplicate by section_number (keep first occurrence)
        seen = set()
        result = []
        for p in paragraphs:
            num = str(p.get("section_number", "")).strip()
            if not num or num in seen:
                continue
            seen.add(num)
            result.append({
                "section_number": num,
                "section_type": p.get("section_type", "paragraph"),
                "title": str(p.get("title", "")).strip(),
                "content": str(p.get("content", "")).strip(),
            })

        return result

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        """Remove ```json ... ``` wrappers if the LLM added them."""
        match = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", text, re.DOTALL)
        if match:
            return match.group(1)
        return text.strip()


def extract_paragraphs_from_chunk(chunk_text: str, extractor: LLMExtractor = None) -> list[dict]:
    """Convenience function: extract paragraphs from a single chunk."""
    extractor = extractor or LLMExtractor()
    return extractor.extract(chunk_text)
