"""Rule-based document type classifier. Transparent on purpose: it returns the cue phrases it used."""
import re

DOC_TYPES = {
    "nit": "Tender Notice (NIT)",
    "boq": "Bill of Quantities (BOQ)",
    "admin_approval": "Administrative Approval",
    "technical_sanction": "Technical Sanction",
    "loa": "Letter of Acceptance",
    "work_order": "Work Order",
    "agreement": "Contractor Agreement",
    "measurement_book": "Measurement Book / Running Bill",
    "payment_record": "Payment Record",
    "completion_certificate": "Completion Certificate",
    "other": "Other / unclassified",
}
# Old labels used by the first prototype and its UI
LEGACY = {"tender": "nit", "award": "loa", "bill": "measurement_book", "completion": "completion_certificate"}

CUES = {
    "nit": [("notice inviting tender", 6), ("nit no", 3), ("invitation for bids", 5), ("invites e-tenders", 5),
            ("e-tender notice", 5), ("tender notification", 4), ("last date for submission", 3),
            ("earnest money deposit", 3), (r"\bemd\b", 1), ("pre-bid", 2), ("bid submission", 2),
            ("tender document fee", 2), ("tenders are invited", 5), ("sealed tenders", 3)],
    "boq": [("bill of quantities", 6), ("schedule of quantities", 6), (r"\bboq\b", 3), ("unit rate", 1),
            (r"\bqty\b|quantity", 1), ("grand total", 2), ("schedule-b", 2), (r"sl\.? ?no.{0,40}description.{0,60}(qty|quantity)", 3)],
    "admin_approval": [("administrative approval", 7), ("administrative sanction", 6), ("hereby accorded", 3),
                       ("approval of the competent authority", 3), ("accorded approval", 4)],
    "technical_sanction": [("technical sanction", 7), ("technically sanctioned", 6), ("technical approval", 3),
                           ("detailed estimate", 2), ("technical scrutiny", 3)],
    "loa": [("letter of acceptance", 7), (r"\bloa\b", 2), ("tender has been accepted", 6), ("accepted your bid", 5),
            ("acceptance of tender", 5), ("performance security", 3), ("performance guarantee", 3),
            ("execute the agreement", 3), ("successful bidder", 2)],
    "work_order": [("pleased to award", 5), ("award of work", 4), ("you are requested to commence", 4), ("work order", 5), ("hereby directed to commence", 6), ("commence the work", 3),
                   ("date of commencement", 2), ("stipulated period", 2), ("work is hereby awarded", 4),
                   ("hereby awarded", 3), ("work order no", 3), ("w\\.o\\. no", 3)],
    "agreement": [("articles of agreement", 7), ("agreement between", 5), ("contract agreement", 6), ("witnesseth", 6),
                  ("first party", 2), ("second party", 2), ("conditions of contract", 3), ("agreement no", 3)],
    "measurement_book": [("measurement book", 7), (r"\bm\.?b\.? no", 4), ("running account bill", 7), (r"\bra bill\b", 5),
                         ("running bill", 6), ("abstract of cost", 3), ("recorded measurements", 4),
                         ("final bill", 4), ("measurements recorded", 4)],
    "payment_record": [("released on", 3), ("sum of rs", 2), ("payment advice", 7), ("payment voucher", 7), ("payment details", 3), ("amount paid", 4),
                       ("net amount payable", 3), (r"\bneft\b", 2), (r"\butr\b", 2), ("cheque no", 2),
                       ("bill paid", 3), ("payment certificate", 5), ("payment record", 6)],
    "completion_certificate": [("completion certificate", 8), ("certificate of completion", 8),
                               ("certified that the work", 6), ("work has been completed", 5),
                               ("completed satisfactorily", 5), ("date of completion", 2),
                               ("defect liability period", 2), ("taken over", 2)],
}
MIN_SCORE = 5


def _title_text(page_text):
    """Heading-like lines near the top: short and mostly upper-case ('NOTICE INVITING TENDER (NIT)').
    Table labels such as 'Work Order No' sit in the first lines too but are not titles."""
    out = []
    for l in [l.strip() for l in page_text.splitlines() if l.strip()][:8]:
        letters = [c for c in l if c.isalpha()]
        if letters and len(l) < 90 and sum(c.isupper() for c in letters) / len(letters) >= 0.7:
            out.append(l)
        elif 2 <= len(l.split()) <= 4 and len(l) < 40 and not re.search(r"[\d:,.;]", l) and l.istitle():
            out.append(l)  # short Title Case line such as 'Payment Advice' 
    return "\n".join(out).lower()


def classify_text(pages_text):
    """pages_text: list of page strings (first pages matter most). -> dict(type, confidence, scores, cues)."""
    joined = re.sub(r"\s+", " ", "\n".join(pages_text[:4])).lower()  # phrases wrap across lines in PDFs
    first_lines = _title_text(pages_text[0]) if pages_text else ""
    scores, cues = {}, {}
    for t, items in CUES.items():
        total, hit = 0, []
        for pat, w in items:
            rx = re.compile(pat if any(ch in pat for ch in "\\[]()|?") else re.escape(pat))
            if rx.search(joined):
                boost = 2 if rx.search(first_lines) else 1  # a cue in the title counts double
                total += w * boost
                hit.append({"cue": pat, "weight": w * boost, "in_title": boost == 2})
        scores[t], cues[t] = total, hit
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    (top_t, top), (_, second) = ranked[0], ranked[1]
    if top < MIN_SCORE:
        return {"type": "other", "confidence": round(0.2 if top else 0.05, 2), "scores": scores, "cues": []}
    conf = max(0.1, min(0.99, 1 - (second + 1) / (top + 1)))
    return {"type": top_t, "confidence": round(conf, 2), "scores": scores, "cues": cues[top_t]}
