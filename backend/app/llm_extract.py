"""Optional LLM-assisted extraction.

The LLM never has the last word: every value it returns must occur literally (after whitespace /
comma normalisation) in the text of the page it cites, otherwise it is discarded. This is how the
"do not invent missing information" rule is enforced for model output.

Configure with ANTHROPIC_API_KEY (and optionally LLM_MODEL). Without a key the rule-based
pipeline runs alone. Tests use MockProvider.
"""
import json, os, re
from .textnorm import parse_amount, parse_date, clean_id

FIELDS = ["tender_id", "job_number", "work_id", "work_order_id", "work_description", "road_name", "location", "ward",
          "department", "contractor", "estimated_cost", "awarded_amount", "work_order_date", "commencement_date",
          "completion_deadline", "payment_amount", "document_date"]
KINDS = {"estimated_cost": "amount", "awarded_amount": "amount", "payment_amount": "amount",
         "work_order_date": "date", "commencement_date": "date", "completion_deadline": "date", "document_date": "date"}
PROMPT = """You extract fields from one page of an Indian municipal (BBMP / GBA) document.
Return ONLY a JSON object. Keys must be a subset of: {fields}.
Each value must be copied EXACTLY as written on the page (no reformatting, no translation, no inference).
Omit any field that is not explicitly present. Never guess.
PAGE TEXT:
{text}"""


class MockProvider:
    """Deterministic stand-in used in tests: returns the dict it was constructed with."""
    def __init__(self, answer): self.answer = answer
    def complete(self, prompt): return json.dumps(self.answer)


class AnthropicProvider:
    def __init__(self, api_key=None, model=None):
        self.key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.model = model or os.getenv("LLM_MODEL", "claude-sonnet-5-5")

    @property
    def available(self): return bool(self.key)

    def complete(self, prompt):
        import httpx
        r = httpx.post("https://api.anthropic.com/v1/messages", timeout=60,
                       headers={"x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                       json={"model": self.model, "max_tokens": 1200, "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", []))


def _squash(s):
    return re.sub(r"[\s,]+", "", str(s)).lower()


def occurs_on_page(value, page_text):
    """True if value appears in the page text, ignoring whitespace and thousands commas."""
    return bool(value) and _squash(value) in _squash(page_text)


def _norm(field, raw):
    kind = KINDS.get(field)
    if kind == "amount":
        a = parse_amount(raw)
        return None if a is None else f"{a:.2f}".rstrip("0").rstrip(".")
    if kind == "date":
        d = parse_date(raw)
        return d.date().isoformat() if d else None
    if field.endswith(("_id", "_number")):
        return clean_id(raw)
    if field == "ward":
        mm = re.search(r"\b(\d{1,3})\b", raw)
        return mm.group(1) if mm else raw.strip()
    return re.sub(r"\s+", " ", raw).strip()


def augment(rows, pages, provider=None):
    """Add validated LLM answers to rule-based rows. Returns (rows, llm_used)."""
    provider = provider or AnthropicProvider()
    if hasattr(provider, "available") and not provider.available:
        return rows, False
    out = list(rows)
    answered = 0  # pages for which the model gave a usable answer; a dead network or a bad reply leaves the rule-based result untouched
    for page_no, text in pages:
        if len(text.strip()) < 30:
            continue
        try:
            ans = json.loads(re.search(r"\{.*\}", provider.complete(PROMPT.format(fields=", ".join(FIELDS), text=text[:12000])), re.S).group(0))
            if not isinstance(ans, dict):
                continue
        except Exception:
            continue
        answered += 1
        for field, raw in ans.items():
            if field not in FIELDS or raw in (None, "") or not isinstance(raw, (str, int, float)):
                continue
            raw = str(raw)
            if not occurs_on_page(raw, text):
                continue  # hallucination guard: not literally on this page
            norm = _norm(field, raw)
            if not norm:
                continue
            same = [r for r in out if r["field"] == field and r["page"] == page_no and str(r["norm_value"]).lower() == norm.lower()]
            if same:
                same[0]["confidence"] = round(min(0.99, same[0]["confidence"] + 0.05), 2)
                same[0]["method"] = same[0]["method"] + "+llm"
            else:
                out.append({"field": field, "raw_value": raw[:500], "norm_value": norm[:500], "page": page_no,
                            "confidence": 0.7, "method": "llm", "snippet": "LLM answer verified present on page"})
    return out, answered > 0
