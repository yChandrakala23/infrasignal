"""Bill of Quantities (BOQ) tables -> structured items.

Reading the table
  Strategy per page, first one that finds a BOQ table wins:
    1. PyMuPDF `find_tables()` on ruled tables. Only the cell *geometry* is used; the text of each cell is read back with
       `get_textbox()`, because the text PyMuPDF returns inside `Table.extract()` loses spaces and splits numbers like 12,000.
    2. PyMuPDF `find_tables(strategy="text")` for tables without ruling lines.
    3. pdfplumber (ruled, then text) as a second opinion.
  A column header row is recognised by its words (S.No / Description / Unit / Qty / Rate / Amount and common variants).
  - Tables that continue over several pages: later pages without a header reuse the previous page's column layout.
  - Two blocks side by side (each with its own header): split at the repeated header and read left block, then right block.
  - A description that wraps onto the next row (empty item number and no numbers) is joined to the item above.
  - Section headings are skipped; "Sub total" / "Total" / "Grand total" rows are stored as such and checked.

Checking the arithmetic (nothing is silently corrected)
  calc_total = quantity x unit_rate. When the printed amount differs by more than rounding, the item is flagged 'mismatch'
  and both numbers are kept. A printed total is compared with the sum of the printed item amounts (or of the printed
  sub-totals). Items whose numbers could not be read are 'incomplete'. A scanned BOQ without a text layer is reported as
  not readable here instead of guessed.
"""
import re
from sqlalchemy.orm import Session
from . import models as m

TOL_ABS = 1.0      # rupees: amounts are usually printed rounded to the rupee
TOL_REL = 1e-6

HEADER_WORDS = {
    "item_no": ["slno", "sno", "srno", "serialno", "itemno", "sl", "slno.", "no"],
    "description": ["description", "descriptionofitem", "descriptionofwork", "particulars", "itemofwork", "itemdescription", "nameofitem", "workitem", "details", "item"],
    "unit": ["unit", "units", "uom"],
    "quantity": ["quantity", "qty", "quantities"],
    "unit_rate": ["rate", "unitrate", "ratersunit", "rateperunit"],
    "total": ["amount", "total", "totalamount", "value", "amountrs"],
}
TOTAL_ROW = re.compile(r"^\s*(sub\s*-?\s*total|grand\s*total|total(\s+amount)?|net\s+total|carried\s+(over|forward)|brought\s+forward|c/o|b/f)\b", re.I)


# ----------------------------------------------------------------------------- small parsers
def clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def classify_header(cell):
    c = re.sub(r"[^a-z]", "", clean(cell).lower().replace("₹", "rs"))
    if not c:
        return None
    if c in ("unitrate", "rateperunit") or ("rate" in c and "unit" in c) or c.startswith("rate"):
        return "unit_rate"
    if c.startswith(("desc", "particular", "nameofitem", "itemofwork")) or "description" in c:
        return "description"
    if c in ("qty", "quantity", "quantities") or c.startswith("qty") or "quant" in c:
        return "quantity"
    if c.startswith(("amount", "totalamount")) or c in ("total", "amount", "value"):
        return "total"
    if c in ("unit", "units", "uom"):
        return "unit"
    if c.startswith(("slno", "sno", "srno", "serialno", "itemno")) or c in ("sl", "no", "slno"):
        return "item_no"
    if c in ("item", "details", "workitem"):
        return "description"
    return None


def number(s):
    """Strict number: 1,25,000.50 / Rs. 4,500 / 12.5 -> float. Anything else (LS, NIL, -, 1.2.3, '12,000 12,000') -> None."""
    t = clean(s)
    t = re.sub(r"^(₹|rs\.?|inr)\s*", "", t, flags=re.I).replace(",", "")
    if t.startswith("(") and t.endswith(")"):
        t = "-" + t[1:-1]
    return float(t) if re.fullmatch(r"-?\d+(\.\d+)?", t) else None


def header_map(row):
    """column index -> canonical field if this row is a BOQ header, else None.
    A field may appear twice when two blocks sit side by side; split_blocks() sorts that out."""
    m_ = {}
    for i, c in enumerate(row):
        k = classify_header(c)
        if k:
            m_[i] = k
    keys = set(m_.values())
    numeric = keys & {"quantity", "unit_rate", "total", "unit"}
    if "description" in keys and len(numeric) >= 2:
        return m_
    return None


def split_blocks(hmap):
    """Blocks side by side repeat the first header field. Returns a list of {column: field} layouts, one per block.
    Without a repeat, a field that appears twice keeps its first column."""
    cols = sorted(hmap)
    first_key = hmap[cols[0]]
    again = [i for i in cols[1:] if hmap[i] == first_key]
    if again:
        cuts = [cols[0]] + again + [10 ** 6]
        blocks = []
        for lo, hi in zip(cuts, cuts[1:]):
            lay, seen = {}, set()
            for i in cols:
                if lo <= i < hi and hmap[i] not in seen:
                    lay[i] = hmap[i]; seen.add(hmap[i])
            blocks.append(lay)
        return blocks
    lay, seen = {}, set()
    for i in cols:
        if hmap[i] not in seen:
            lay[i] = hmap[i]; seen.add(hmap[i])
    return [lay]


# ----------------------------------------------------------------------------- reading tables
def _tables_pymupdf(page, strategy):
    try:
        tf = page.find_tables(strategy=strategy) if strategy != "lines" else page.find_tables()
    except Exception:
        return []
    out = []
    for tb in tf.tables:
        rows = []
        for r in tb.rows:
            rows.append([clean(page.get_textbox(_rect(c))) if c else "" for c in r.cells])
        if len(rows) >= 2:
            out.append(rows)
    return out


def _rect(c):
    import fitz
    return fitz.Rect(c)


def _tables_plumber(plumber_page, strategy):
    if plumber_page is None:
        return []
    out = []
    try:
        for t in plumber_page.extract_tables({"vertical_strategy": strategy, "horizontal_strategy": strategy}):
            rows = [[clean(c) for c in r] for r in t]
            if len(rows) >= 2:
                out.append(rows)
    except Exception:
        return []
    return out


def _parse_table(rows, state, page_no, table_index, method):
    """rows -> items. `state` carries the column layout between tables/pages (continued tables)."""
    items, hdr_at = [], None
    layouts = None
    for i, r in enumerate(rows[:4]):
        h = header_map(r)
        if h:
            layouts, hdr_at = split_blocks(h), i
            state["layouts"], state["ncols"] = layouts, len(r)
            break
    if layouts is None:
        if state.get("layouts") and rows and len(rows[0]) == state.get("ncols"):
            layouts = state["layouts"]; hdr_at = -1  # continued table: no header repeated on this page
            method = method + "+continued"
        else:
            return []
    body = rows[hdr_at + 1:]
    for bi, lay in enumerate(layouts):
        last = None
        for ri, r in enumerate(body):
            cells = {k: clean(r[i]) if i < len(r) else "" for i, k in lay.items()}
            if not any(cells.values()):
                continue
            if all(re.fullmatch(r"\(?\s*(rs\.?|₹|in rs\.?|nos\.?)?\s*\)?", v, re.I) for v in cells.values()) :
                continue
            desc = cells.get("description", "")
            label = desc or cells.get("item_no", "")
            qty_s, rate_s, tot_s, unit = cells.get("quantity", ""), cells.get("unit_rate", ""), cells.get("total", ""), cells.get("unit", "")
            has_num = any(v for v in (qty_s, rate_s, tot_s, unit))
            base = {"page": page_no, "table_index": table_index, "row_index": ri + bi * 1000, "method": method, "raw": [clean(c) for c in r],
                    "confidence": 0.9 if method.startswith("pymupdf_table") else 0.8 if method.startswith("pdfplumber") else 0.7}
            if TOTAL_ROW.match(label) or TOTAL_ROW.match(cells.get("item_no", "")):
                kind = "subtotal" if re.match(r"\s*(sub|carried|brought|c/o|b/f)", label or cells.get("item_no", ""), re.I) else "total"
                items.append({**base, "row_kind": kind, "item_no": None, "description": clean(label), "unit": None, "quantity": None, "unit_rate": None,
                              "total_amount": number(tot_s) if number(tot_s) is not None else number(rate_s) if number(rate_s) is not None else number(qty_s)})
                last = None
                continue
            if not has_num:
                if cells.get("item_no") and not desc.strip():
                    continue
                if not cells.get("item_no") and desc and last is not None:
                    last["description"] = clean(last["description"] + " " + desc)  # wrapped description
                    last["raw"].append(desc)
                    continue
                continue  # section heading
            it = {**base, "row_kind": "item", "item_no": cells.get("item_no") or None, "description": desc, "unit": unit or None,
                  "quantity": number(qty_s), "unit_rate": number(rate_s), "total_amount": number(tot_s),
                  "_bad": {k: v for k, v in (("quantity", qty_s), ("unit_rate", rate_s), ("total_amount", tot_s)) if v and number(v) is None and not re.fullmatch(r"[-–—nNiIlL.\s]*", v)}}
            items.append(it)
            last = it
    return items


def _score(items):
    """How trustworthy a reading is. A row counts for it when quantity x rate equals its printed amount, and against it when that fails,
    when it repeats the numbers of the row before it, or when its item number is not a short label. Readers that glue neighbouring
    rows together produce exactly those rows, so they lose to a reader that sees the table properly."""
    its = [dict(i) for i in items]
    validate(its)
    good = bad = 0
    prev = None
    for i in its:
        if i["row_kind"] != "item":
            continue
        triple = (i.get("page"), i.get("quantity"), i.get("unit_rate"), i.get("total_amount"))
        glued = triple == prev and triple[1] is not None
        weird_no = bool(i.get("item_no")) and (" " in i["item_no"] or len(i["item_no"]) > 12)
        if i.get("check_status") == "ok" and not glued and not weird_no:
            good += 1
        else:
            bad += 1
        prev = triple
    return (good - bad, good)


def extract_boq(path):
    """-> {"items": [...], "pages": n, "strategies": {page: method}, "warnings": [...]}"""
    import fitz
    doc = fitz.open(path)
    out, strategies, warnings = [], {}, []
    state = {}
    text_pages = 0
    try:
        import pdfplumber
        plumber = pdfplumber.open(path)
    except Exception:  # pdfplumber missing or the file confuses it: PyMuPDF alone still works
        plumber = None
    for pno in range(len(doc)):
        page = doc[pno]
        ppage = plumber.pages[pno] if plumber is not None and pno < len(plumber.pages) else None
        if len(page.get_text().strip()) >= 30:
            text_pages += 1
        # Run every reader and keep the one whose rows agree best with their own arithmetic (a reader that half-sees a table or
        # glues rows together loses). Ties go to the earlier reader, so PyMuPDF's ruled-table reading is preferred.
        best, best_state, best_method = [], state, None
        readers = (("pymupdf_table", lambda: _tables_pymupdf(page, "lines")),
                   ("pymupdf_text_table", lambda: _tables_pymupdf(page, "text")),
                   ("pdfplumber_table", lambda: _tables_plumber(ppage, "lines")),
                   ("pdfplumber_text_table", lambda: _tables_plumber(ppage, "text")))
        for method, fn in readers:
            st = dict(state)
            res = []
            for ti, rows in enumerate(fn()):
                res += _parse_table(rows, st, pno + 1, ti, method)
            if res and (not best or _score(res) > _score(best)):
                best, best_state, best_method = res, st, method
        got = best
        if got:
            strategies[pno + 1] = best_method
            state.clear(); state.update(best_state)
        out += got
    if plumber is not None:
        plumber.close()
    if text_pages == 0:
        warnings.append("This document has no text layer (a scan). BOQ tables are not read from scanned pages; enter or correct them by hand.")
    elif not out:
        warnings.append("No BOQ table with a recognisable header (description plus quantity / rate / amount) was found.")
    return {"items": out, "pages": len(doc), "strategies": strategies, "warnings": warnings}


# ----------------------------------------------------------------------------- arithmetic checks
def _close(a, b):
    return abs(a - b) <= max(TOL_ABS, TOL_REL * max(abs(a), abs(b)))


def validate(items):
    """Fills calc_total / check_status / check_note in place; returns a summary."""
    running, sub_running, subtotals = 0.0, 0.0, []
    n_items = n_bad = 0
    printed_total = None
    for it in items:
        if it["row_kind"] == "item":
            n_items += 1
            q, r, t = it.get("quantity"), it.get("unit_rate"), it.get("total_amount")
            it["calc_total"] = round(q * r, 2) if q is not None and r is not None else None
            notes = []
            bad = it.get("_bad") or {}
            if bad:
                notes.append("not a number: " + ", ".join(f"{k}='{v}'" for k, v in bad.items()))
            if it["calc_total"] is not None and t is not None:
                if _close(it["calc_total"], t):
                    it["check_status"] = "ok"
                else:
                    it["check_status"] = "mismatch"
                    notes.append(f"printed amount {t:,.2f} but quantity x rate = {it['calc_total']:,.2f} (difference {t - it['calc_total']:,.2f})")
            else:
                it["check_status"] = "incomplete"
                missing = [n for n, v in (("quantity", q), ("rate", r), ("amount", t)) if v is None]
                notes.append("cannot check: " + ", ".join(missing) + " not read")
            if bad and it["check_status"] == "ok":
                it["check_status"] = "incomplete"
            it["check_note"] = "; ".join(notes)[:300] or None
            if it["check_status"] != "ok":
                n_bad += 1
            if t is not None:
                running += t; sub_running += t
        elif it["row_kind"] == "subtotal":
            t = it.get("total_amount")
            if t is None:
                it["check_status"], it["check_note"] = "incomplete", "sub-total amount not read"
            elif _close(sub_running, t) or _close(running, t):
                it["check_status"], it["check_note"] = "ok", None
            else:
                it["check_status"] = "mismatch"
                it["check_note"] = f"printed sub-total {t:,.2f} but the items above add up to {sub_running:,.2f} (this section) / {running:,.2f} (so far)"
            it["calc_total"] = round(sub_running, 2)
            subtotals.append(t if t is not None else 0.0); sub_running = 0.0
        else:  # grand total
            t = it.get("total_amount")
            it["calc_total"] = round(running, 2)
            if t is None:
                it["check_status"], it["check_note"] = "incomplete", "total amount not read"
            elif _close(running, t) or (subtotals and _close(sum(subtotals), t)):
                it["check_status"], it["check_note"] = "ok", None
            else:
                it["check_status"] = "mismatch"
                it["check_note"] = f"printed total {t:,.2f} but the item amounts add up to {running:,.2f} (difference {t - running:,.2f})"
            printed_total = t if t is not None else printed_total
    flagged_totals = sum(1 for i in items if i["row_kind"] != "item" and i.get("check_status") == "mismatch")
    return {"items": n_items, "items_with_issues": n_bad, "mismatches": sum(1 for i in items if i.get("check_status") == "mismatch"),
            "incomplete": sum(1 for i in items if i.get("check_status") == "incomplete"), "total_mismatches": flagged_totals,
            "sum_of_item_amounts": round(running, 2), "printed_total": printed_total}


# ----------------------------------------------------------------------------- storing
def store(db: Session, doc: m.Document, replace_reviewed=False):
    """Read the BOQ tables of a stored document and (re)build its items. Reviewed items are kept unless replace_reviewed."""
    reviewed = db.query(m.BoqItem).filter(m.BoqItem.document_id == doc.id, m.BoqItem.status != "unverified").count()
    if reviewed and not replace_reviewed:
        raise ValueError("This document has reviewed BOQ items. Re-reading would discard them; confirm to replace.")
    res = extract_boq(doc.path)
    summary = validate(res["items"])
    db.query(m.BoqItem).filter(m.BoqItem.document_id == doc.id).delete(synchronize_session=False)
    for it in res["items"]:
        db.add(m.BoqItem(document_id=doc.id, page=it["page"], table_index=it["table_index"], row_index=it["row_index"], row_kind=it["row_kind"],
                         item_no=(it.get("item_no") or None) and it["item_no"][:40], description=it.get("description"), unit=(it.get("unit") or None) and it["unit"][:40], quantity=it.get("quantity"),
                         unit_rate=it.get("unit_rate"), total_amount=it.get("total_amount"), calc_total=it.get("calc_total"),
                         check_status=it.get("check_status"), check_note=(it.get("check_note") or None) and it["check_note"][:300], method=it["method"].replace("pdfplumber", "plumber")[:30], confidence=it["confidence"],
                         raw=it["raw"], status="unverified"))
    db.flush()
    return {**summary, "strategies": res["strategies"], "warnings": res["warnings"], "pages": res["pages"]}


def revalidate_stored(db: Session, document_id: int):
    """After an officer corrects an item, recompute every check for the document from the stored values."""
    rows = db.query(m.BoqItem).filter(m.BoqItem.document_id == document_id, m.BoqItem.status != "rejected").order_by(m.BoqItem.page, m.BoqItem.row_index).all()
    items = [{"row_kind": r.row_kind, "quantity": r.quantity, "unit_rate": r.unit_rate, "total_amount": r.total_amount} for r in rows]
    summary = validate(items)
    for r, it in zip(rows, items):
        r.calc_total, r.check_status, r.check_note = it.get("calc_total"), it.get("check_status"), it.get("check_note")
    return summary


def summary_for(db: Session, document_id: int):
    rows = db.query(m.BoqItem).filter(m.BoqItem.document_id == document_id, m.BoqItem.status != "rejected").all()
    items = [r for r in rows if r.row_kind == "item"]
    totals = [r for r in rows if r.row_kind == "total"]
    return {"items": len(items), "items_with_issues": sum(1 for r in items if r.check_status != "ok"),
            "mismatches": sum(1 for r in rows if r.check_status == "mismatch"), "incomplete": sum(1 for r in rows if r.check_status == "incomplete"),
            "sum_of_item_amounts": round(sum(r.total_amount or 0 for r in items), 2),
            "printed_total": totals[-1].total_amount if totals else None,
            "reviewed": sum(1 for r in rows if r.status in ("verified", "corrected"))}
