"""Generates the SAMPLE datasets under data/sample and data/eval.  Deterministic (seeded).

!! EVERYTHING GENERATED HERE IS SYNTHETIC !!
Work orders, tenders, contractors, job numbers and PDFs are invented, modelled on the *shape* of BBMP
OpenCity / KPPP data so the pipelines can be demonstrated offline. Contractor names are obviously fictional
("Sample ...", "Demo ..."). Road names and ward numbers/names are real (wards come from the real 243-ward
boundary file; which ward a sample road passes through is computed from the sample road geometry).
Every row carries Data Status = SAMPLE and every PDF carries a SAMPLE banner.

Outputs
  data/sample/sample_work_orders_243.csv      work orders + payments (OpenCity-like columns)
  data/sample/sample_tenders_kppp.csv         tenders (KPPP-like columns)
  data/sample/documents/*.pdf                 19 synthetic documents (one scanned, for OCR)
  data/eval/extraction_gold.json              field values printed in each PDF
  data/eval/matching_gold.json                doc->work, tender->work, work->road ground truth
  data/eval/complaints_labelled.json          labelled synthetic complaints for clustering evaluation
"""
import csv, io, json, os, random, sys, tempfile
from datetime import date, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
tmpdb = tempfile.mktemp(suffix=".db")
os.environ["DATABASE_URL"] = "sqlite:///" + tmpdb
from app.db import init_db, SessionLocal  # noqa: E402
from app import models as m, wards, roads  # noqa: E402
from app.textnorm import clean_id, norm_road_name  # noqa: E402

from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak  # noqa: E402

random.seed(42)
OUT = BACKEND / "data"
(OUT / "sample" / "documents").mkdir(parents=True, exist_ok=True)
(OUT / "eval").mkdir(parents=True, exist_ok=True)

# ----------------------------------------------------------------- geography from the real ward file
init_db()
db = SessionLocal()
wards.load_ward_file(db, BACKEND / "data/wards/wards_243.geojson", "243", "DataMeet/KGIS")
roads.import_road_file(db, BACKEND / "data/roads/sample_major_roads.geojson", "SAMPLE_APPROX", is_approx=True, is_demo=True)
ROAD_WARDS = {}  # road name -> [(ward number, ward name, metres)] longest first
for r in db.query(m.Road).filter(m.Road.name.isnot(None)):
    agg = {}
    for s in db.query(m.RoadSegment).filter_by(road_id=r.id):
        if s.ward_number:
            k = (s.ward_number, s.ward_name)
            agg[k] = agg.get(k, 0) + s.length_m
    ROAD_WARDS[r.name] = sorted(((k[0], k[1], v) for k, v in agg.items()), key=lambda x: -x[2])
ROAD_MID = {r.name: (r.centroid_lat, r.centroid_lon) for r in db.query(m.Road).filter(m.Road.name.isnot(None))}
SEG_BY_ROAD = {}
for r in db.query(m.Road):
    SEG_BY_ROAD[r.id] = [(s.mid_lat, s.mid_lon, s.ward_number) for s in db.query(m.RoadSegment).filter_by(road_id=r.id)]
ROAD_ID = {r.name: r.id for r in db.query(m.Road).filter(m.Road.name.isnot(None))}
UNNAMED_IDS = [r.id for r in db.query(m.Road).filter(m.Road.name.is_(None))]

CONTRACTORS = ["Sample Infra Builders Pvt Ltd", "Demo Road Works Co", "Placeholder Constructions Ltd",
               "Example Asphalt and Paving Pvt Ltd", "Test Bengaluru Civil Works", "Sample Greenline Contractors",
               "Mock Highway Services Pvt Ltd", "Illustrative Roadways Co"]
DEPTS = ["Roads Infrastructure (sample)", "Major Roads (sample)", "Ward Engineering (sample)", "Project Central (sample)"]


def D(y, mth, d):
    return date(y, mth, d)


def fy_of(d):
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y}-{str(y + 1)[2:]}"


# ----------------------------------------------------------------- work orders
# (key, road, ward_rank, kind, description, wo_date, estimate, awarded, contractor_idx, tender?, completion_deadline, n_bills, paid_fraction)
SPEC = [
    ("A", "Hosur Road", 0, "resurfacing", "Asphalting of Hosur Road from Dairy Circle to Silk Board junction", D(2024, 6, 12), 38500000, 36200000, 0, True, D(2024, 12, 12), 3, 1.0),
    ("B", "Outer Ring Road (Hebbal to Silk Board section)", 0, "resurfacing", "Resurfacing of Outer Ring Road service lane and main carriageway near Marathahalli bridge", D(2025, 2, 5), 52000000, 49800000, 1, True, D(2025, 8, 5), 2, 0.6),
    ("C", "MG Road", 0, "white_topping", "White topping of MG Road from Anil Kumble Circle to Trinity Circle", D(2024, 9, 20), 61000000, 58900000, 2, True, D(2025, 3, 20), 2, 0.8),
    ("D", "Sarjapur Road", 0, "resurfacing", "Asphalting of Sarjapur Road from Agara junction towards Bellandur", D(2025, 5, 14), 21800000, 20100000, 3, True, D(2025, 11, 14), 1, 0.5),
    ("E", "Kanakapura Road", 0, "resurfacing", "Improvements and asphalting of Kanakapura Road from South End Circle", D(2025, 7, 1), 17400000, 16600000, 4, True, D(2026, 1, 1), 1, 0.0),
    ("F", "Bannerghatta Road", 0, "resurfacing", "Asphalting of Bannerghatta Road from Dairy Circle to Jayadeva junction", D(2025, 8, 18), 27300000, 25900000, 5, False, D(2026, 2, 18), 1, 0.3),
    ("G", "100 Feet Road Indiranagar", 0, "pothole_repair", "Pothole repair and patch work on 100 Feet Road Indiranagar", D(2025, 10, 3), 2400000, 2250000, 6, False, D(2025, 11, 3), 1, 1.0),
    ("H", "80 Feet Road Koramangala", 0, "dug_road_restoration", "Restoration of dug up road on 80 Feet Road Koramangala after utility works", D(2025, 11, 20), 3100000, 2950000, 7, False, D(2026, 1, 20), 1, 0.4),
    ("I", "Bellary Road", 0, "resurfacing", "Resurfacing of Bellary Road from Mekhri Circle to Hebbal flyover", D(2024, 4, 22), 44700000, 43100000, 0, True, D(2024, 10, 22), 2, 1.0),
    ("J", "Bellary Road", 1, "pothole_repair", "Pothole filling on Bellary Road near Hebbal", D(2025, 12, 9), 1800000, 1700000, 1, False, D(2026, 1, 9), 1, 0.0),
    ("K", "Old Madras Road", 0, "resurfacing", "Asphalting of Old Madras Road from Trinity Circle to Indiranagar metro", D(2024, 11, 11), 31800000, 30400000, 2, True, D(2025, 5, 11), 2, 0.9),
    ("L", "Old Madras Road", 1, "white_topping", "White topping of Old Madras Road near KR Puram", D(2026, 1, 15), 72000000, 69500000, 3, True, D(2026, 12, 15), 1, 0.1),
    ("M", "Mysore Road", 0, "resurfacing", "Asphalting of Mysore Road from City Market to Nayandahalli", D(2023, 8, 7), 36100000, 34800000, 4, True, D(2024, 2, 7), 2, 1.0),
    ("N", "Mysore Road", 1, "dug_road_restoration", "Restoration of roads dug up for cable laying on Mysore Road", D(2026, 3, 2), 4200000, 3950000, 5, False, D(2026, 6, 2), 1, 0.0),
    ("O", "Tumkur Road", 0, "resurfacing", "Resurfacing of Tumkur Road from Yeshwanthpur circle towards Peenya", D(2025, 1, 16), 25600000, 24300000, 6, True, D(2025, 7, 16), 1, 0.7),
    ("P", "Magadi Road", 0, "resurfacing", "Asphalting of Magadi Road from Kamakshipalya to Basaveshwara Nagar", D(2024, 3, 12), 19500000, 18400000, 7, True, D(2024, 9, 12), 1, 1.0),
    ("Q", "Whitefield Main Road", 0, "resurfacing", "Asphalting of Whitefield Main Road from Mahadevapura to ITPL", D(2026, 4, 10), 33000000, 31700000, 0, True, D(2027, 1, 10), 1, 0.0),
    ("R", "Hosur Road", 1, "pothole_repair", "Pothole repair on Hosur Road near Bommanahalli", D(2026, 5, 6), 2900000, 2700000, 1, False, D(2026, 8, 6), 1, 0.5),
    ("S", "Outer Ring Road (Hebbal to Silk Board section)", 1, "dug_road_restoration", "Restoration of dug up road on Outer Ring Road near Bellandur", D(2026, 6, 18), 5100000, 4800000, 2, False, D(2026, 12, 18), 1, 0.0),
    ("T", "Sarjapur Road", 1, "resurfacing", "Asphalting of Sarjapur Road service road near Haralur", D(2026, 7, 21), 9700000, 9100000, 3, True, D(2027, 2, 21), 1, 0.0),
]
# works with no road name in the description (tests ward-only / no-road-name behaviour)
GENERIC = [
    ("U", 186, "Koramangala", "Pothole filling works in Koramangala ward under ward maintenance", D(2025, 9, 8), 1500000, 1420000, 2, 1.0),
    ("V", 70, "Hebbala", "Providing asphalt patch work to damaged roads in Hebbala ward", D(2026, 2, 12), 2100000, 1990000, 4, 0.5),
]
NON_ROAD = [("Construction of storm water drain in Jakkasandra ward", 191, "Jakkasandra", 6200000), ("Development of park and fencing in Marathahalli ward", 114, "Marathahalli", 3300000),
            ("Lake rejuvenation works at Bellandur", 115, "Bellanduru", 28000000), ("Purchase of office furniture", 120, "Hoysala Nagar", 450000)]
MULTI = [("Major road white topping package (multi-ward) under Major Roads", 301, "Multi-ward", 120000000),
         ("Storm water drain works package (multi-ward)", 307, "Multi-ward", 90000000)]

works = []
for i, (key, road, rank, kind, desc, wod, est, aw, ci, has_t, dead, nbills, paid_frac) in enumerate(SPEC, start=1):
    wr = ROAD_WARDS[road]
    wn, wname = wr[min(rank, len(wr) - 1)][:2]
    n = len(works) + 1
    fy = fy_of(wod)
    job = f"SAMPLE/J/{fy[2:4]}{fy[5:]}/{n:03d}"
    works.append(dict(key=key, job=job, wo_no=f"SAMPLE/WO/{fy}/{n:03d}", wo_date=wod, desc=desc, est=est, aw=aw,
                      contractor=CONTRACTORS[ci], tender=(f"SAMPLE/KPPP/{fy[2:4]}{fy[5:]}/T{n:03d}" if has_t else None),
                      work_id=f"SAMPLE/AGR/{fy}/{n:03d}", dept=DEPTS[i % len(DEPTS)], ward=(wn, wname), category=kind,
                      road=road, deadline=dead, nbills=nbills, paid_frac=paid_frac))
for key, wn, wname, desc, wod, est, aw, ci, paid_frac in GENERIC:
    n = len(works) + 1; fy = fy_of(wod)
    works.append(dict(key=key, job=f"SAMPLE/J/{fy[2:4]}{fy[5:]}/{n:03d}", wo_no=f"SAMPLE/WO/{fy}/{n:03d}", wo_date=wod, desc=desc,
                      est=est, aw=aw, contractor=CONTRACTORS[ci], tender=None, work_id=None, dept=DEPTS[2], ward=(str(wn), wname),
                      category="pothole_repair", road=None, deadline=None, nbills=1, paid_frac=paid_frac))
for desc, wn, wname, amt in NON_ROAD + MULTI:
    n = len(works) + 1; wod = D(2025, random.randint(4, 12), random.randint(1, 28)); fy = fy_of(wod)
    works.append(dict(key=None, job=f"SAMPLE/J/{fy[2:4]}{fy[5:]}/{n:03d}", wo_no=f"SAMPLE/WO/{fy}/{n:03d}", wo_date=wod, desc=desc,
                      est=amt, aw=amt, contractor=random.choice(CONTRACTORS), tender=None, work_id=None, dept=DEPTS[0],
                      ward=(str(wn), wname), category=None, road=None, deadline=None, nbills=1, paid_frac=0.0))
BY_KEY = {w["key"]: w for w in works if w["key"]}


def inr(n):
    return f"{n:,.0f}"


# CSV (OpenCity-like columns + optional extras that some real files do not have)
rows = []
for w in works:
    amt_paid_total = round(w["aw"] * w["paid_frac"], -2)
    bills = []
    if w["nbills"] and amt_paid_total:
        part = amt_paid_total / w["nbills"]
        for b in range(w["nbills"]):
            bills.append((f"SAMPLE/BILL/{w['job'][-3:]}/{b + 1}", w["wo_date"] + timedelta(days=60 + 45 * b), round(part, -2)))
    elif w["nbills"]:
        bills.append(("", None, None))
    for bn, bd, ba in (bills or [("", None, None)]):
        rows.append({
            "Ward Number": w["ward"][0], "Ward Name": w["ward"][1], "Job Number": w["job"], "Work Order Number": w["wo_no"],
            "Work Order Date": w["wo_date"].strftime("%d/%m/%Y"), "Work Description": w["desc"],
            "Contractor Name": "M/s " + w["contractor"], "Work Order Amount": inr(w["aw"]), "Estimate Amount": inr(w["est"]),
            "Tender ID": w["tender"] or "", "Department": w["dept"],
            "Completion Deadline": w["deadline"].strftime("%d/%m/%Y") if w["deadline"] else "",
            "Bill Number": bn, "Bill Date": bd.strftime("%d/%m/%Y") if bd else "", "Bill Amount": inr(ba) if ba else "",
            "Paid Amount": inr(ba) if ba else "", "Payment Date": (bd + timedelta(days=21)).strftime("%d/%m/%Y") if bd else "",
            "Data Status": "SAMPLE (synthetic, not a government record)"})
with open(OUT / "sample/sample_work_orders_243.csv", "w", newline="", encoding="utf-8") as f:
    wr = csv.DictWriter(f, fieldnames=list(rows[0].keys())); wr.writeheader(); wr.writerows(rows)

# tenders
tenders = []
for w in works:
    if w["tender"]:
        pub = w["wo_date"] - timedelta(days=75)
        tenders.append({"Tender ID": w["tender"], "BBMP Job Number": w["job"] if int(w["job"][-3:]) % 2 == 0 else "", "Title": w["desc"], "Department": w["dept"],
                        "Category": w["category"], "Published Date": pub.strftime("%d/%m/%Y"), "Closing Date": (pub + timedelta(days=21)).strftime("%d/%m/%Y"),
                        "Estimated Value": inr(w["est"]), "Awarded Value": inr(w["aw"]), "Status": "Awarded", "Contractor": "M/s " + w["contractor"],
                        "Location": w["road"] or "", "URL": "", "Data Status": "SAMPLE (synthetic, not a government record)"})
# fuzzy-only tenders: similar title to a work, tender id not referenced by any work, awarded value equal
FUZZY = [("F", "SAMPLE/KPPP/2526/F901", "Asphalting of Bannerghatta Road between Dairy Circle and Jayadeva junction"),
         ("G", "SAMPLE/KPPP/2526/F902", "Patch work and pothole repair, 100 Feet Road, Indiranagar"),
         ("H", "SAMPLE/KPPP/2526/F903", "Restoration of dug-up road, 80 Feet Road, Koramangala"),
         ("J", "SAMPLE/KPPP/2526/F904", "Pothole filling Bellary Road, Hebbal")]
for key, tid, title in FUZZY:
    w = BY_KEY[key]; pub = w["wo_date"] - timedelta(days=70)
    tenders.append({"Tender ID": tid, "BBMP Job Number": "", "Title": title, "Department": w["dept"], "Category": w["category"],
                    "Published Date": pub.strftime("%d/%m/%Y"), "Closing Date": (pub + timedelta(days=15)).strftime("%d/%m/%Y"),
                    "Estimated Value": inr(w["est"]), "Awarded Value": inr(w["aw"]), "Status": "Awarded", "Contractor": "M/s " + w["contractor"],
                    "Location": "", "URL": "", "Data Status": "SAMPLE (synthetic, not a government record)"})
ORPHANS = [("SAMPLE/KPPP/2526/X801", "Widening of Hennur Main Road with drains and footpaths", 98000000, "Published"),
           ("SAMPLE/KPPP/2526/X802", "Supply of bitumen emulsion for ward road maintenance", 7600000, "Closed"),
           ("SAMPLE/KPPP/2627/X803", "Resurfacing of internal roads of Jayanagar 4th block", 14800000, "Published"),
           ("SAMPLE/KPPP/2526/X804", "Asphalting of Hennur Bagalur Road (cancelled)", 22000000, "Cancelled")]
for tid, title, v, st in ORPHANS:
    pub = D(2026, random.randint(1, 8), random.randint(1, 25))
    tenders.append({"Tender ID": tid, "BBMP Job Number": "", "Title": title, "Department": DEPTS[1], "Category": "resurfacing", "Published Date": pub.strftime("%d/%m/%Y"),
                    "Closing Date": (pub + timedelta(days=21)).strftime("%d/%m/%Y"), "Estimated Value": inr(v), "Awarded Value": "" if st != "Closed" else inr(v * 0.96),
                    "Status": st, "Contractor": "", "Location": "", "URL": "", "Data Status": "SAMPLE (synthetic, not a government record)"})
with open(OUT / "sample/sample_tenders_kppp.csv", "w", newline="", encoding="utf-8") as f:
    wr = csv.DictWriter(f, fieldnames=list(tenders[0].keys())); wr.writeheader(); wr.writerows(tenders)

# ----------------------------------------------------------------- documents
ss = getSampleStyleSheet()
H = ParagraphStyle("H", parent=ss["Title"], fontSize=15, spaceAfter=6)
BANNER = ParagraphStyle("B", parent=ss["Normal"], fontSize=8, textColor=colors.red, alignment=1)
P = ParagraphStyle("P", parent=ss["Normal"], fontSize=10.5, leading=15)


def banner():
    return Paragraph("SAMPLE DOCUMENT - SYNTHETIC, NOT AN OFFICIAL RECORD. Generated for software demonstration.", BANNER)


def kv_table(pairs):
    cell = ParagraphStyle("cell", parent=ss["Normal"], fontSize=10, leading=13)
    t = Table([[Paragraph(str(k), cell), Paragraph(str(v), cell)] for k, v in pairs], colWidths=[150, 330])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 10),
                           ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return t


def build_pdf(name, story):
    path = OUT / "sample/documents" / name
    SimpleDocTemplate(str(path), pagesize=A4, topMargin=40, bottomMargin=40, leftMargin=50, rightMargin=50).build(story)
    return path


def money(v):
    return f"Rs. {inr(v)}/-"


def dstr(d, style="slash"):
    return d.strftime("%d/%m/%Y") if style == "slash" else d.strftime("%d %B %Y")


GOLD, MATCH_DOCS = {}, {}


def gold(name, dtype, fields, work_key, note=""):
    GOLD[name] = {"doc_type": dtype, "fields": {k: v for k, v in fields.items() if v is not None}}
    MATCH_DOCS[name] = {"work_job": BY_KEY[work_key]["job"] if work_key else None, "note": note}


def iso(d):
    return d.isoformat()


def letter(name, dtype, title, w, lines, extra=None, work_key=None, gold_fields=None, date_=None, note=""):
    story = [banner(), Spacer(1, 6), Paragraph("Roads Infrastructure Division (sample issuer)", ss["Heading4"]), Paragraph(title, H), Spacer(1, 8)]
    for ln in lines:
        story.append(Paragraph(ln, P))
    build_pdf(name, story)
    gold(name, dtype, gold_fields or {}, work_key or w["key"], note)


def tpl_work_order(w, name, style="table", ward=True, ids=True):
    pairs = [("Work Order No", w["wo_no"]), ("Work Order Date", dstr(w["wo_date"])), ("BBMP Job No", w["job"])]
    if w["tender"] and ids: pairs.append(("Tender ID", w["tender"]))
    pairs += [("Name of Work", w["desc"]), ("Ward No", f"{w['ward'][0]} ({w['ward'][1]})"), ("Department", w["dept"]),
              ("Contractor", "M/s " + w["contractor"]), ("Contract Amount", money(w["aw"])),
              ("Date of commencement", dstr(w["wo_date"] + timedelta(days=10))), ("Date of completion", dstr(w["deadline"]) if w["deadline"] else "N/A")]
    if not ids:
        pairs = [p for p in pairs if p[0] not in ("BBMP Job No", "Work Order No", "Tender ID")]
    story = [banner(), Spacer(1, 6), Paragraph("WORK ORDER", H), Spacer(1, 8), kv_table(pairs), Spacer(1, 12),
             Paragraph("The contractor is hereby directed to commence the work and complete it within the stipulated period as per the "
                       "conditions of contract. This is a sample document.", P)]
    build_pdf(name, story)
    f = {"work_order_id": clean_id(w["wo_no"]) if ids else None, "work_order_date": iso(w["wo_date"]), "job_number": clean_id(w["job"]) if ids else None,
         "tender_id": clean_id(w["tender"]) if (w["tender"] and ids) else None, "work_description": w["desc"], "ward": w["ward"][0],
         "department": w["dept"], "contractor": w["contractor"], "awarded_amount": str(w["aw"]),
         "commencement_date": iso(w["wo_date"] + timedelta(days=10)), "completion_deadline": iso(w["deadline"]) if w["deadline"] else None}
    return f


def payment_advice(w, name, amount, pdate):
    pairs = [("Job No", w["job"]), ("Work Order No", w["wo_no"]), ("Payee Name", "M/s " + w["contractor"]), ("Name of Work", w["desc"]),
             ("Ward No", w["ward"][0]), ("Amount Paid", money(amount)), ("Payment Date", dstr(pdate)), ("UTR", "SAMPLEUTR" + str(random.randint(10**8, 10**9)))]
    story = [banner(), Spacer(1, 6), Paragraph("PAYMENT ADVICE", H), Spacer(1, 8), kv_table(pairs), Spacer(1, 10),
             Paragraph("Payment released against the bill. Payment does not by itself certify physical completion of work.", P)]
    build_pdf(name, story)
    gold(name, "payment_record", {"job_number": clean_id(w["job"]), "work_order_id": clean_id(w["wo_no"]), "contractor": w["contractor"], "work_description": w["desc"],
                                  "ward": w["ward"][0], "payment_amount": str(amount), "payment_date": iso(pdate)}, w["key"])


# ---- Work A: the full 10-document pack
w = BY_KEY["A"]
tw = w["wo_date"] - timedelta(days=75)
nit_story = [banner(), Spacer(1, 6), Paragraph("NOTICE INVITING TENDER (NIT)", H),
             kv_table([("Tender ID", w["tender"]), ("BBMP Job No", w["job"]), ("Name of Work", w["desc"]),
                       ("Ward No", f"{w['ward'][0]} ({w['ward'][1]})"), ("Department", w["dept"]), ("Estimated Cost", money(w["est"])),
                       ("Last date for submission", dstr(tw + timedelta(days=21)))]),
             Spacer(1, 10), Paragraph("Sealed tenders are invited from eligible contractors. Earnest Money Deposit as per tender conditions.", P), PageBreak(),
             banner(), Paragraph("Tender conditions (page 2)", ss["Heading3"]),
             Paragraph("Bidders must submit bid documents through the e-procurement portal. Pre-bid meeting details are on the portal.", P), Spacer(1, 6),
             Paragraph(f"Date: {dstr(tw)}", P)]
build_pdf("A01_nit_hosur_road.pdf", nit_story)
gold("A01_nit_hosur_road.pdf", "nit", {"tender_id": clean_id(w["tender"]), "job_number": clean_id(w["job"]), "work_description": w["desc"], "ward": w["ward"][0], "department": w["dept"],
                                         "estimated_cost": str(w["est"]), "document_date": iso(tw)}, "A")
items = [("1", "Bituminous concrete 40mm", "sqm", 12000, 1450), ("2", "Dense bituminous macadam 50mm", "sqm", 12000, 1900), ("3", "Tack coat", "sqm", 24000, 38), ("4", "Road marking", "m", 3500, 95)]
tot = sum(q * r for _, _, _, q, r in items)
boq = [banner(), Spacer(1, 6), Paragraph("BILL OF QUANTITIES", H), Paragraph(f"Name of Work: {w['desc']}", P), Paragraph(f"Tender ID: {w['tender']}", P), Spacer(1, 8)]
t = Table([["Sl No", "Description", "Unit", "Qty", "Rate", "Amount"]] + [[a, b, c, str(inr(d)), str(inr(e)), str(inr(d * e))] for a, b, c, d, e in items] + [["", "Grand Total", "", "", "", inr(tot)]], colWidths=[40, 190, 40, 60, 60, 80])
t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 9)]))
boq += [t, Spacer(1, 8), Paragraph(f"Grand Total: Rs. {inr(tot)}/-", P)]
build_pdf("A02_boq_hosur_road.pdf", boq)
gold("A02_boq_hosur_road.pdf", "boq", {"tender_id": clean_id(w["tender"]), "work_description": w["desc"]}, "A", "BOQ total is not equal to the estimate on purpose")
letter("A03_admin_approval_hosur_road.pdf", "admin_approval", "ADMINISTRATIVE APPROVAL", w,
       [f"Date: {dstr(w['wo_date'] - timedelta(days=150))}", f"Job No: {w['job']}", f"Sub: Administrative approval for {w['desc']}",
        f"Ward No: {w['ward'][0]}", f"Department: {w['dept']}", f"Estimated cost: {money(w['est'])}",
        "Administrative approval is hereby accorded by the competent authority for the above work."],
       gold_fields={"document_date": iso(w["wo_date"] - timedelta(days=150)), "job_number": clean_id(w["job"]), "work_description": w["desc"], "ward": w["ward"][0],
                    "department": w["dept"], "estimated_cost": str(w["est"])})
letter("A04_technical_sanction_hosur_road.pdf", "technical_sanction", "TECHNICAL SANCTION", w,
       [f"Date: {dstr(w['wo_date'] - timedelta(days=120))}", f"Job No: {w['job']}", f"Name of Work: {w['desc']}", f"Sanctioned estimate: {money(w['est'])}",
        "Technical sanction is accorded to the detailed estimate after technical scrutiny."],
       gold_fields={"document_date": iso(w["wo_date"] - timedelta(days=120)), "job_number": clean_id(w["job"]), "work_description": w["desc"], "estimated_cost": str(w["est"])})
letter("A05_loa_hosur_road.pdf", "loa", "LETTER OF ACCEPTANCE", w,
       [f"Date: {dstr(w['wo_date'] - timedelta(days=12))}", f"To, M/s {w['contractor']}", f"Tender ID: {w['tender']}", f"Job No: {w['job']}", f"Name of Work: {w['desc']}",
        f"Awarded contract amount: {money(w['aw'])}", "Your tender has been accepted. Submit the performance security and execute the agreement within 14 days."],
       gold_fields={"document_date": iso(w["wo_date"] - timedelta(days=12)), "tender_id": clean_id(w["tender"]), "job_number": clean_id(w["job"]), "work_description": w["desc"],
                    "awarded_amount": str(w["aw"])})
f = tpl_work_order(w, "A06_work_order_hosur_road.pdf")
gold("A06_work_order_hosur_road.pdf", "work_order", f, "A")
letter("A07_agreement_hosur_road.pdf", "agreement", "ARTICLES OF AGREEMENT", w,
       [f"Agreement No: {w['work_id']}", f"Date: {dstr(w['wo_date'] + timedelta(days=5))}", f"This agreement between the Corporation (first party) and M/s {w['contractor']} (second party)",
        f"Name of Work: {w['desc']}", f"Job No: {w['job']}", f"Contract value: {money(w['aw'])}", "Witnesseth that the parties agree to the conditions of contract."],
       gold_fields={"work_id": clean_id(w["work_id"]), "document_date": iso(w["wo_date"] + timedelta(days=5)), "work_description": w["desc"], "job_number": clean_id(w["job"]),
                    "awarded_amount": str(w["aw"])})
rb = round(w["aw"] / 3)
s = [banner(), Spacer(1, 6), Paragraph("RUNNING ACCOUNT BILL (RA BILL) - MEASUREMENT BOOK", H),
     kv_table([("Job No", w["job"]), ("Work Order No", w["wo_no"]), ("Bill No", "SAMPLE/RA/001"), ("Name of Work", w["desc"]),
               ("Contractor", "M/s " + w["contractor"]), ("Net amount payable", money(rb)), ("Date", dstr(w["wo_date"] + timedelta(days=90)))])]
build_pdf("A08_running_bill_hosur_road.pdf", s)
gold("A08_running_bill_hosur_road.pdf", "measurement_book", {"job_number": clean_id(w["job"]), "work_order_id": clean_id(w["wo_no"]), "work_description": w["desc"],
                                                              "contractor": w["contractor"], "payment_amount": str(rb), "document_date": iso(w["wo_date"] + timedelta(days=90))}, "A")
payment_advice(w, "A09_payment_hosur_road.pdf", 12000000, w["wo_date"] + timedelta(days=120))
letter("A10_completion_hosur_road.pdf", "completion_certificate", "COMPLETION CERTIFICATE", w,
       [f"Date: {dstr(w['deadline'] + timedelta(days=9))}", f"Job No: {w['job']}", f"Name of Work: {w['desc']}", f"Date of completion: {dstr(w['deadline'] + timedelta(days=4))}",
        "It is certified that the work has been completed satisfactorily. The defect liability period starts from the date of completion."],
       gold_fields={"document_date": iso(w["deadline"] + timedelta(days=9)), "job_number": clean_id(w["job"]), "work_description": w["desc"], "completion_deadline": iso(w["deadline"] + timedelta(days=4))})
# NB: 'Date of completion' on a certificate is the actual completion date; it is extracted as the completion_deadline label by design
#     and the gold file reflects the label semantics, so the evaluation measures extraction, not interpretation.

# ---- Work B: NIT + WO + payment (second template)
w = BY_KEY["B"]
nit_b = [banner(), Spacer(1, 6), Paragraph("TENDER NOTIFICATION", H),
         kv_table([("Tender No", w["tender"]), ("Job No", w["job"]), ("Name of Work", w["desc"]), ("Ward", f"{w['ward'][0]} ({w['ward'][1]})"), ("Executing Agency", w["dept"]),
                   ("Tender Value", money(w["est"]))]), Spacer(1, 8),
         Paragraph("Tenders are invited. Earnest money deposit and bid submission rules apply.", P), Paragraph(f"Dated: {dstr(w['wo_date'] - timedelta(days=80))}", P)]
build_pdf("B01_nit_orr.pdf", nit_b)
gold("B01_nit_orr.pdf", "nit", {"tender_id": clean_id(w["tender"]), "job_number": clean_id(w["job"]), "work_description": w["desc"], "ward": w["ward"][0], "department": w["dept"],
                                  "estimated_cost": str(w["est"]), "document_date": iso(w["wo_date"] - timedelta(days=80))}, "B")
gold("B02_work_order_orr.pdf", "work_order", tpl_work_order(w, "B02_work_order_orr.pdf"), "B")
payment_advice(w, "B03_payment_orr.pdf", 29000000, w["wo_date"] + timedelta(days=100))

# ---- Work C: scanned work order (image only -> OCR) + payment
w = BY_KEY["C"]
tmp_pdf = tpl_work_order(w, "_tmp_c.pdf")
import fitz  # noqa: E402
from PIL import Image, ImageFilter  # noqa: E402
src = fitz.open(OUT / "sample/documents/_tmp_c.pdf")
pix = src[0].get_pixmap(dpi=170)
img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L").filter(ImageFilter.GaussianBlur(0.6))
img = img.rotate(0.4, expand=False, fillcolor=255)
buf = io.BytesIO(); img.save(buf, "JPEG", quality=60)
out = fitz.open(); pg = out.new_page(width=pix.width * 72 / 170, height=pix.height * 72 / 170)
pg.insert_image(pg.rect, stream=buf.getvalue())
out.save(str(OUT / "sample/documents/C01_work_order_mg_road_SCANNED.pdf")); out.close(); src.close()
os.remove(OUT / "sample/documents/_tmp_c.pdf")
gold("C01_work_order_mg_road_SCANNED.pdf", "work_order", tmp_pdf, "C", "scanned image only: exercises OCR")
payment_advice(w, "C02_payment_mg_road.pdf", 47100000, w["wo_date"] + timedelta(days=150))

# ---- Work D: conflict: job number belongs to D, tender id belongs to E
wD, wE = BY_KEY["D"], BY_KEY["E"]
letter("D01_loa_conflict.pdf", "loa", "LETTER OF ACCEPTANCE", wD,
       [f"Date: {dstr(wD['wo_date'] - timedelta(days=9))}", f"To, M/s {wD['contractor']}", f"Tender ID: {wE['tender']}", f"Job No: {wD['job']}",
        f"Name of Work: {wD['desc']}", f"Awarded contract amount: {money(wD['aw'])}", "Your tender has been accepted."],
       gold_fields={"document_date": iso(wD["wo_date"] - timedelta(days=9)), "tender_id": clean_id(wE["tender"]), "job_number": clean_id(wD["job"]), "work_description": wD["desc"],
                    "awarded_amount": str(wD["aw"])}, note="CONFLICT: job number points to work D, tender id points to work E. Must not be auto-merged.")

# ---- orphan NIT, fuzzy-only work order, unclassifiable letter
tid, title = ORPHANS[0][0], ORPHANS[0][1]
orph = [banner(), Spacer(1, 6), Paragraph("NOTICE INVITING TENDER (NIT)", H),
        kv_table([("Tender ID", tid), ("Name of Work", title), ("Department", DEPTS[1]), ("Estimated Cost", money(98000000))]),
        Paragraph("Tenders are invited. Last date for submission and EMD details are on the portal.", P), Paragraph("Date: 14/03/2026", P)]
build_pdf("X01_nit_orphan_hennur.pdf", orph)
gold("X01_nit_orphan_hennur.pdf", "nit", {"tender_id": clean_id(tid), "work_description": title, "department": DEPTS[1], "estimated_cost": "98000000", "document_date": "2026-03-14"}, None, "orphan: tender not in any work")
wF = BY_KEY["F"]
fuzzy_story = [banner(), Spacer(1, 6), Paragraph("WORK ORDER", H),
               kv_table([("Name of Work", "Asphalting of Bannerghatta Road between Dairy Circle and Jayadeva junction"), ("Contractor", "M/s " + wF["contractor"]),
                         ("Location", "Bannerghatta Road"), ("Contract Amount", money(wF["aw"])), ("Work Order Date", dstr(wF["wo_date"]))]),
               Paragraph("The contractor is hereby directed to commence the work. No job or tender number is quoted on this copy.", P)]
build_pdf("F01_work_order_no_ids.pdf", fuzzy_story)
gold("F01_work_order_no_ids.pdf", "work_order", {"work_description": "Asphalting of Bannerghatta Road between Dairy Circle and Jayadeva junction", "contractor": wF["contractor"],
                                                  "location": "Bannerghatta Road", "awarded_amount": str(wF["aw"]), "work_order_date": iso(wF["wo_date"])}, "F",
     "no identifiers: only contractor + road + description + amount can link it (fuzzy, needs manual verification)")
other = [banner(), Spacer(1, 6), Paragraph("MINUTES OF REVIEW MEETING", H),
         Paragraph("Date: 02/02/2026", P), Paragraph("Attendees discussed monsoon preparedness, desilting schedules and general coordination between departments. No decisions about specific works were recorded.", P)]
build_pdf("Z01_meeting_minutes_other.pdf", other)
gold("Z01_meeting_minutes_other.pdf", "other", {"document_date": "2026-02-02"}, None, "should stay unclassified and unmatched")

# ----------------------------------------------------------------- gold files
tender_gold = {}
for t_ in tenders:
    tid = t_["Tender ID"]
    job = next((w_["job"] for w_ in works if w_["tender"] == tid), None)
    if job is None:
        for key, ftid, _ in FUZZY:
            if ftid == tid:
                job = BY_KEY[key]["job"]
    tender_gold[tid] = job
road_gold = {w_["job"]: w_["road"] for w_ in works if w_["road"]}
json.dump({"documents": MATCH_DOCS, "tenders": tender_gold, "work_road": road_gold, "no_road_expected": [w_["job"] for w_ in works if not w_["road"]]},
          open(OUT / "eval/matching_gold.json", "w"), indent=1)
json.dump({"note": "Field values printed in the SAMPLE PDFs. Synthetic, so scores measure the pipeline on easy documents, not real-world accuracy.", "documents": GOLD},
          open(OUT / "eval/extraction_gold.json", "w"), indent=1)

# ----------------------------------------------------------------- labelled complaints (for clustering evaluation)
CAT_TEXT = {
    "pothole": ["Large pothole near {lm}", "Deep pothole, two wheelers skid here", "There is a big hole in the road at {lm}", "Crater like pothole, dangerous at night",
                "pothole came back after the repair", "Potholes all over this stretch near {lm}", "pothole"],
    "damaged_road": ["Road surface broken and uneven near {lm}", "Asphalt peeled off and loose gravel on road", "Road damaged after digging, not restored", "Broken tar road near {lm}"],
    "waterlogging": ["Water logging here after every rain near {lm}", "Road flooded, drain blocked", "Stagnant water on the road for days near {lm}", "Water stagnation damaging the road"],
    "incomplete_work": ["Road work left incomplete near {lm}", "Work started and abandoned, material lying", "Half finished road work, trench open near {lm}"],
    "footpath": ["Footpath slabs broken near {lm}", "Pavement damaged, pedestrians walk on road", "Broken walkway near {lm}"],
    "service_lane": ["Service lane full of potholes near {lm}", "Service road not maintained, garbage and holes"],
}
LANDMARKS = ["the bus stop", "the signal", "the metro station", "the school gate", "the temple", "the petrol bunk"]


def jitter(lat, lon, m_):
    return lat + random.uniform(-m_, m_) / 110540.0, lon + random.uniform(-m_, m_) / 108000.0


def seg_point(road_name, k):
    rid = ROAD_ID[road_name]
    segs = SEG_BY_ROAD[rid]
    s = segs[min(k, len(segs) - 1)]
    return s[0], s[1]


HOTSPOTS = [  # (label, road, seg index, category, n reports, spread_days, expect_alert)
    ("H1", "Hosur Road", 3, "pothole", 8, 40, True), ("H2", "Outer Ring Road (Hebbal to Silk Board section)", 12, "damaged_road", 6, 30, True),
    ("H3", "Sarjapur Road", 2, "incomplete_work", 5, 25, True), ("H4", "Bellary Road", 5, "pothole", 4, 14, True),
    ("H5", "Mysore Road", 4, "waterlogging", 5, 20, True), ("H6", "Residency Road", 1, "pothole", 4, 18, True),
    ("H7", "Cunningham Road", 1, "footpath", 3, 10, True), ("H8", "Sampige Road", 1, "damaged_road", 3, 9, True),
    ("H9", "Dr Rajkumar Road", 1, "pothole", 4, 12, True), ("H10", "Brigade Road", 0, "damaged_road", 3, 8, True),
    ("H11", "100 Feet Road Indiranagar", 1, "pothole", 3, 6, True), ("H12", "Tumkur Road", 3, "service_lane", 3, 7, True),
]
comp, gid = [], 0
for label, road, k, cat, n, span, alert in HOTSPOTS:
    la, lo = seg_point(road, k)
    for i in range(n):
        txt = random.choice(CAT_TEXT[cat]).format(lm=random.choice(LANDMARKS))
        lat, lon = jitter(la, lo, 14)
        comp.append({"lat": round(lat, 6), "lon": round(lon, 6), "category": cat, "description": txt, "days_ago": random.randint(1, span) if i else 1, "group": label})
# decoys
la, lo = seg_point("Hosur Road", 3)  # same place as H1, different issue: should NOT merge into H1
for i in range(2):
    lat, lon = jitter(la, lo, 8)
    comp.append({"lat": round(lat, 6), "lon": round(lon, 6), "category": "waterlogging", "description": random.choice(CAT_TEXT["waterlogging"]).format(lm="the bus stop"), "days_ago": random.randint(2, 10), "group": "D1"})
la, lo = seg_point("Bannerghatta Road", 2)  # same place, 200 days apart: should NOT merge
for dago in (5, 205):
    lat, lon = jitter(la, lo, 6)
    comp.append({"lat": round(lat, 6), "lon": round(lon, 6), "category": "pothole", "description": "Pothole near the signal", "days_ago": dago, "group": None})
for i in range(24):  # scattered singles across Bengaluru
    rname = random.choice(list(ROAD_ID))
    la, lo = seg_point(rname, random.randint(0, 6))
    lat, lon = jitter(la, lo, 60)
    cat = random.choice(list(CAT_TEXT))
    comp.append({"lat": round(lat, 6), "lon": round(lon, 6), "category": cat, "description": random.choice(CAT_TEXT[cat]).format(lm=random.choice(LANDMARKS)), "days_ago": random.randint(1, 80), "group": None})
off = [(12.9085, 77.5700, "Pothole in the internal layout road, JP Nagar"), (12.9560, 77.6560, "Broken road in layout lane near Domlur")]  # not near any mapped road
for la, lo, t_ in off:
    for i in range(3):
        lat, lon = jitter(la, lo, 10)
        comp.append({"lat": round(lat, 6), "lon": round(lon, 6), "category": "pothole", "description": t_, "days_ago": random.randint(1, 15), "group": f"OFF{la}"})
groups = {}
for c in comp:
    if c["group"]:
        groups.setdefault(c["group"], []).append(c)
true_groups = {g: {"n": len(v), "distinct_days": len({x["days_ago"] for x in v}), "expect_alert": len(v) >= 3 and g.startswith(("H", "OFF"))} for g, v in groups.items()}
json.dump({"note": "SYNTHETIC complaints with hand-set true groups. 'group' is the ground-truth cluster (null = isolated report). days_ago is relative to the moment of loading.",
           "complaints": comp, "groups": true_groups}, open(OUT / "eval/complaints_labelled.json", "w"), indent=1)
print(f"works: {len(works)}  csv rows: {len(rows)}  tenders: {len(tenders)}  docs: {len(GOLD)}  complaints: {len(comp)}  groups: {len(groups)}")
db.close(); os.remove(tmpdb)
