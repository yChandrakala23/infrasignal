"""Builds six SYNTHETIC narrative-style PDFs under data/heldout/ plus their answer key.
They are written in prose (letters, paragraphs) instead of the 'Label: value' tables of the main sample pack, to give an
honest second reading of extraction quality. They are NOT loaded by the seed script and were NOT used to tune the extractor."""
import json
from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

OUT = Path(__file__).resolve().parents[1] / "data"
(OUT / "heldout").mkdir(exist_ok=True)
S = getSampleStyleSheet()
BANNER = "SAMPLE: synthetic document for software testing. Not a government record."

DOCS = {
 "H01_work_order_letter.pdf": ("work_order", [
  "Bruhat Bengaluru Mahanagara Palike, Dasarahalli Zone", "Office of the Executive Engineer (Roads)",
  "No. SAMPLE/WO/HX/101 &nbsp;&nbsp;&nbsp; Date: 14.08.2025",
  "To,<br/>M/s Demo Highways Pvt. Ltd.,<br/>Peenya Industrial Area, Bengaluru",
  "Sub: Award of work - Asphalting of Kodigehalli Main Road, Ward No. 12 - reg.",
  "Ref: 1) Tender Notification No. SAMPLE/KPPP/2526/H01 &nbsp; 2) BBMP Job No. SAMPLE/J/2526/H01",
  "Sir,",
  "With reference to the above, I am directed to inform you that the Commissioner is pleased to award the work of "
  "'Asphalting of Kodigehalli Main Road from the railway gate to the Sahakara Nagar junction' to you at a contract value of "
  "Rs. 84,50,000/- (Rupees eighty four lakhs fifty thousand only). You are requested to commence the work on or before 25.08.2025 "
  "and complete it on or before 11.12.2025."],
  {"work_order_id": "SAMPLE/WO/HX/101", "work_order_date": "2025-08-14", "tender_id": "SAMPLE/KPPP/2526/H01", "job_number": "SAMPLE/J/2526/H01",
   "contractor": "Demo Highways Pvt Ltd", "awarded_amount": "8450000", "ward": "12", "commencement_date": "2025-08-25", "completion_deadline": "2025-12-11"}),
 "H02_tender_notice_prose.pdf": ("nit", [
  "E-PROCUREMENT NOTICE", "Tender Notification No.: SAMPLE/KPPP/2526/H02",
  "The Executive Engineer, Yelahanka Division invites e-tenders from eligible registered contractors for the work titled "
  "'Resurfacing of Jakkur Main Road between Jakkur Aerodrome gate and Amruthahalli' (Job No. SAMPLE/J/2526/H02), Ward No. 4.",
  "The estimated cost put to tender is Rs. 1.25 Crore. Earnest money deposit is 1% of the estimate. Bids must be submitted on or before 05.09.2025 up to 5 PM. "
  "Tender documents may be downloaded from the e-procurement portal."],
  {"tender_id": "SAMPLE/KPPP/2526/H02", "job_number": "SAMPLE/J/2526/H02", "estimated_cost": "12500000", "ward": "4", "document_date": None}),
 "H03_payment_release_prose.pdf": ("payment_record", [
  "Payment Advice", "Office of the Accounts Officer, Mahadevapura Zone",
  "This is to record that a sum of Rs. 18,75,000/- (Rupees eighteen lakhs seventy five thousand only) was released on 19-Sep-2025 to "
  "M/s Demo Roadways (India) Pvt Ltd towards the second running account bill for the work 'Pothole repair on Whitefield Main Road near ITPL junction', "
  "executed under Work Order No. SAMPLE/WO/HX/103 dated 01.07.2025 (Job No. SAMPLE/J/2526/H03)."],
  {"payment_amount": "1875000", "payment_date": "2025-09-19", "contractor": "Demo Roadways (India) Pvt Ltd", "work_order_id": "SAMPLE/WO/HX/103", "job_number": "SAMPLE/J/2526/H03"}),
 "H04_completion_certificate_prose.pdf": ("completion_certificate", [
  "CERTIFICATE OF COMPLETION", "Office of the Assistant Executive Engineer, Bommanahalli Sub-Division",
  "It is certified that the work of 'Dug road restoration at Hosur Main Road service lane near Singasandra' executed by M/s Sample Restoration Works "
  "under Work Order No. SAMPLE/WO/HX/104 (Job No. SAMPLE/J/2526/H04) was completed satisfactorily and taken over on 28th October 2025."],
  {"work_order_id": "SAMPLE/WO/HX/104", "job_number": "SAMPLE/J/2526/H04", "contractor": "Sample Restoration Works", "document_date": "2025-10-28"}),
 "H05_acceptance_letter_prose.pdf": ("loa", [
  "LETTER OF ACCEPTANCE", "Ref. No. SAMPLE/LOA/H05/2025 dated 02 July 2025",
  "To M/s Placeholder Civil Engineers, Rajajinagar.",
  "Your bid against Tender No. SAMPLE/KPPP/2526/H05 for 'Improvement of Magadi Road from Tollgate to Sumanahalli' has been accepted by the competent authority "
  "at Rs. 2,10,00,000/- (Rupees two crore ten lakhs only). You are requested to furnish the performance security and execute the agreement within 15 days."],
  {"tender_id": "SAMPLE/KPPP/2526/H05", "contractor": "Placeholder Civil Engineers", "awarded_amount": "21000000", "document_date": "2025-07-02"}),
 "H06_admin_approval_prose.pdf": ("admin_approval", [
  "GOVERNMENT OF KARNATAKA (SAMPLE)", "Order No. SAMPLE/AA/H06/2025, Bengaluru, dated 21.05.2025",
  "Administrative approval is hereby accorded for an amount of Rs. 2.4 Crores for the work of 'Asphalting of Sarjapur Road service road from Agara to Iblur', "
  "Ward No. 150, under the Roads Infrastructure programme for the year 2025-26."],
  {"estimated_cost": "24000000", "ward": "150", "document_date": "2025-05-21"}),
}
gold = {}
for name, (dtype, paras, fields) in DOCS.items():
    story = [Paragraph(f"<font color='#8a5a00' size=9>{BANNER}</font>", S["Normal"]), Spacer(1, 10)]
    for i, p in enumerate(paras):
        story += [Paragraph(p, S["Title"] if i == 0 and p.isupper() else S["Normal"]), Spacer(1, 8)]
    SimpleDocTemplate(str(OUT / "heldout" / name), pagesize=A4).build(story)
    gold[name] = {"doc_type": dtype, "fields": {k: v for k, v in fields.items() if v is not None}}
json.dump({"note": "Narrative-style SYNTHETIC documents, written after the extractor was tuned and not used to tune it.", "documents": gold},
          open(OUT / "eval" / "extraction_heldout_gold.json", "w"), indent=1)
print("built", len(gold))
