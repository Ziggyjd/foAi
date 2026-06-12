"""
ai_extract.py — Inspection report understanding
================================================
Takes a free-text inspection report and extracts STRUCTURED compliance data:
    { result, severity, summary, model_used }

Primary engine: a HuggingFace zero-shot classification model (real ML, runs
locally/offline after a one-time weight download — no API key required).

Fallback engine: a deterministic keyword classifier that runs with zero extra
dependencies, so the app NEVER crashes if the model can't be downloaded (e.g.
no internet on first run). The UI shows which engine produced the result.

This is the AI component of SafeKeep: unstructured text -> structured record.
"""

# Candidate labels the model chooses between (zero-shot = no training needed)
RESULT_LABELS = ["Pass", "Defect noted", "Fail"]
SEVERITY_LABELS = ["minor", "moderate", "critical"]

# Lazy global so the (heavy) model loads once, only when first needed.
_classifier = None
_load_failed = False

# Model is small (~250MB) and CPU-friendly. distilbert-mnli does zero-shot well.
_MODEL_NAME = "typeform/distilbert-base-uncased-mnli"


def _get_classifier():
    """Load the zero-shot pipeline once. Returns None if it can't be loaded."""
    global _classifier, _load_failed
    if _classifier is not None:
        return _classifier
    if _load_failed:
        return None
    try:
        from transformers import pipeline
        _classifier = pipeline("zero-shot-classification", model=_MODEL_NAME)
        return _classifier
    except Exception as e:  # no internet / no torch / etc.
        print(f"[ai_extract] ML model unavailable, using fallback: {e}")
        _load_failed = True
        return None


# ---------------------------------------------------------------------------
# Deterministic fallback (also used as a sanity layer)
# ---------------------------------------------------------------------------
_FAIL_WORDS = ["fail", "failed", "non-compliant", "not compliant", "unsafe",
               "did not operate", "inoperable", "out of service", "no flow"]
_DEFECT_WORDS = ["defect", "fault", "leak", "perished", "low pressure", "damaged",
                 "corrosion", "rust", "blocked", "obstructed", "expired", "replace",
                 "recommend", "follow-up", "follow up", "missing"]
_CRITICAL_WORDS = ["unsafe", "fail", "inoperable", "no flow", "fire risk",
                   "immediate", "urgent", "evacuat"]
_MODERATE_WORDS = ["leak", "low pressure", "damaged", "blocked", "replace", "perished"]


_NEGATERS = ["no ", "not ", "without ", "free of ", "nil ", "zero "]


def _has_unnegated(text: str, words) -> bool:
    """True if any keyword appears NOT immediately preceded by a negation.
    Handles cases like 'no defects found' which should NOT count as a defect."""
    for w in words:
        start = 0
        while True:
            i = text.find(w, start)
            if i == -1:
                break
            preceding = text[max(0, i - 12):i]
            if not any(neg in preceding for neg in _NEGATERS):
                return True
            start = i + len(w)
    return False


def _fallback_extract(text: str) -> dict:
    t = text.lower()
    if _has_unnegated(t, _FAIL_WORDS):
        result = "Fail"
    elif _has_unnegated(t, _DEFECT_WORDS):
        result = "Defect noted"
    else:
        result = "Pass"

    if result == "Pass":
        severity = "none"
    elif _has_unnegated(t, _CRITICAL_WORDS):
        severity = "critical"
    elif _has_unnegated(t, _MODERATE_WORDS):
        severity = "moderate"
    else:
        severity = "minor"

    return {
        "result": result,
        "severity": severity,
        "summary": _summarise(text),
        "model_used": "rule-based fallback",
    }


def _summarise(text: str, max_len: int = 160) -> str:
    """Cheap extractive summary: first sentence(s) up to max_len chars."""
    text = " ".join(text.split())
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    # try to end on a sentence boundary
    for sep in [". ", "; ", ", "]:
        i = cut.rfind(sep)
        if i > 60:
            return cut[: i + 1].strip()
    return cut.rstrip() + "…"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def extract_from_report(text: str) -> dict:
    """Main entry point. Returns structured compliance data from free text.

    Tries the ML model first; falls back to the deterministic classifier if the
    model isn't available. Always returns the same dict shape so callers don't
    care which engine ran.
    """
    text = (text or "").strip()
    if not text:
        return {"result": "Pass", "severity": "none",
                "summary": "", "model_used": "none (empty input)"}

    clf = _get_classifier()
    if clf is None:
        return _fallback_extract(text)

    try:
        result_pred = clf(text, RESULT_LABELS)
        result = result_pred["labels"][0]
        result_conf = round(float(result_pred["scores"][0]), 3)

        # Only assess severity if it's not a clean pass
        if result == "Pass":
            severity = "none"
        else:
            sev_pred = clf(text, SEVERITY_LABELS)
            severity = sev_pred["labels"][0]

        return {
            "result": result,
            "severity": severity,
            "summary": _summarise(text),
            "confidence": result_conf,
            "model_used": f"ML zero-shot ({_MODEL_NAME.split('/')[-1]})",
        }
    except Exception as e:
        print(f"[ai_extract] inference error, using fallback: {e}")
        return _fallback_extract(text)


if __name__ == "__main__":
    # quick manual test of the fallback (model path tested on a machine with net)
    samples = [
        "Attended site and tested all six fire hose reels. Reel 3 on level 2 had "
        "low water pressure and a perished hose. Recommend replacement.",
        "All emergency exit signs illuminated correctly on 90-minute discharge test. "
        "No defects found. System compliant.",
        "Fire pump failed to start on mains power. System inoperable. Building unsafe "
        "until rectified — urgent.",
    ]
    for s in samples:
        print(extract_from_report(s))
