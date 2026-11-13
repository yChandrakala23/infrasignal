"""Builds the BBMP test fixtures (tests/fixtures/bbmp). Run:  python tests/make_bbmp_fixtures.py

IMPORTANT - what these files are: they follow the layout of the real OpenCity BBMP files (column names, the packed wodetails /
contractor / brnumber fields, the odd quoting, the 20-character contractor names). The bill / RTGS reference strings and the contractor strings
are copied from rows read in the real 2023-24, 2024-25 and 2025-26 files; the work descriptions, amounts, ward numbers and work-order
numbers are INVENTED and the data is not a copy of any real file. The HTML pages are reconstructions of the CKAN markup, not saved copies:
the build environment could not download the live pages (see docs/BBMP_COLLECTION.md). Nothing here is, or should be shown as, genuine BBMP data.
"""
import csv, io, random
from pathlib import Path
import openpyxl

OUT = Path(__file__).parent / "fixtures" / "bbmp"
OUT.mkdir(parents=True, exist_ok=True)

# strings in the exact shape seen in the real files (brnumber has no separator before CBR; contractor has the phone glued on)
BR = ["BR - 000042 / 10-Jul-2023CBR - / Rtgs - 004097 / 19-Jun-2024", "BR - 000166 / 28-Feb-2023CBR - / Rtgs - 005555 / 25-Oct-2024",
      "BR - 000089 / 06-Oct-2023CBR - / Rtgs - 004221 / 02-Jul-2024", "BR - 000083 / 16-Sep-2023CBR - / Rtgs - 005218 / 04-Sep-2024",
      "BR - 000104 / 29-Nov-2023CBR - / Rtgs - 001116 / 03-Dec-2024", "BR - 000114 / 21-Oct-2023CBR - / Rtgs - 000902 / 18-Jun-2024"]
CONTR = ["023258 RRENGINEERINGWORKS8880709398", "035531 SRI VENKATESWARA CON9980071159", "023411 NAVEEN K9448708434", "004595 SADIAN9845583321",
         "AKSHAY CONSTRUCTIONS9164858389", "022380 A G NARAYANASWAMY &9738780002", "KRIDL BHUSIRI ACCOU0000000000", "026261 SRINIVAS.M.R 9739988642"]
WORKS = ["Drilling of borewells in {w} ward No-{n:02d}", "Comprehensive Development of Roads & Drains in {w} Layout", "Asphalting of Hosur Road from Silk Board to Madiwala",
         "Desilting of drains at {w} 4th phase", "Construction of Flagging concrete at Govt. Hospital premises in {w} ward", "Improvements of drains at {w} Colony"]
HEAD = ["slno", "id", "Ward", "wodetails", "contractor", "brnumber", "amount", "nett", "deduction"]


def rows(n, regime_ward_max, yy, seed, br_cycle=BR):
    rnd = random.Random(seed)
    out = []
    for i in range(1, n + 1):
        ward = rnd.randint(1, regime_ward_max)
        seq = rnd.randint(1, 12)
        code = f"{ward:03d}-{yy}-{seq:06d}"
        desc = rnd.choice(WORKS).format(w=rnd.choice(["Yelahanka", "Jakkur", "Hebbal", "Madiwala"]), n=ward)
        amount = rnd.randrange(100000, 3000000, 1000)
        ded = round(amount * rnd.choice([0.03, 0.05, 0.08]))
        out.append([i * 3 + 1, 100000 + rnd.randint(1, 99999), ward, f"{code}</a>{desc}", rnd.choice(CONTR), rnd.choice(br_cycle), amount, amount - ded, ded])
    return out


def write_csv(name, header, data, quirks=False):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow(header)
    for r in data:
        w.writerow(r)
    (OUT / name).write_text(buf.getvalue(), encoding="utf-8")


def main():
    # --- 2024-25, 243-ward regime. Includes: a repeated identical row, a bad amount, a negative amount, an unbalanced quote line,
    #     two payments of one work, a row with no work-order code, a row with no payment reference.
    d = rows(40, 243, "23", 1)
    d[5][0], d[5][1] = d[4][0], d[4][1]; d[5][2:] = d[4][2:]                      # identical to the row before: repeated row
    d[7][6] = "twelve thousand"                                                    # not a number
    d[9][6], d[9][7], d[9][8] = -50000, -50000, 0                                  # negative
    d[11][3] = d[10][3].replace("</a>", "</a>") ; d[11][2] = d[10][2]              # same work as row 10 (a second payment)
    d[11][5] = BR[4]
    d[13][3] = "Footpath restoration with no code in the details field"           # no work-order code
    d[15][5] = "BR - 000111 / 01-Aug-2023CBR - / Rtgs - / "                          # bill but no RTGS: not yet paid
    d[17][7], d[17][8] = d[17][6] - 7000, 1000                                     # nett + deduction != amount
    write_csv("wo_payments_2024_25_243.csv", HEAD, d)
    # an unbalanced quote at the start of a field, in the middle of the file (what the real files contain): the csv module would swallow the
    # lines after it, so the reader has to re-read them one by one
    lines = (OUT / "wo_payments_2024_25_243.csv").read_text(encoding="utf-8").split("\n")
    lines.insert(21, '9001,198765,12,"012-23-000099</a>Repair of retaining wall, near school,SRI VENKATESWARA CON9980071159,BR - 000050 / 11-Jan-2024CBR - / Rtgs - 002001 / 12-Apr-2024,450000,420000,30000')
    (OUT / "wo_payments_2024_25_243.csv").write_text("\n".join(lines), encoding="utf-8")
    # --- 2024-25, 198-ward regime: smaller, header spelled "Ward"
    write_csv("wo_payments_2024_25_198.csv", HEAD, rows(15, 198, "22", 2))
    # --- 2023-24: "Ward No" header, work orders, same layout
    h23 = ["slno", "id", "Ward No", "wodetails", "contractor", "brnumber", "amount", "nett", "deduction"]
    d23 = rows(20, 198, "22", 3)
    write_csv("wo_2023_24_198.csv", h23, d23)
    # --- 2025-26 common wards: lower-case "ward", a non-numeric ward ("R"), an empty contractor
    h25 = ["slno", "id", "ward", "wodetails", "contractor", "brnumber", "amount", "nett", "deduction"]
    d25 = rows(12, 390, "24", 4)
    for r in d25:
        r[2] = 300 + (r[2] % 90)
    d25[3][2] = "R"; d25[6][4] = ""
    write_csv("wo_payments_2025_26_common.csv", h25, d25)
    for reg, mx, seed in (("198", 198, 6), ("243", 243, 7), ("225", 225, 8)):
        write_csv(f"wo_payments_2025_26_{reg}.csv", h25, rows(8, mx, "24", seed))
    # --- the same layout as .xlsx (numbers as real numbers, as an Excel export would hold them)
    wb = openpyxl.Workbook(); ws = wb.active; ws.append(HEAD)
    for r in rows(10, 243, "23", 5):
        ws.append(r)
    wb.save(OUT / "wo_payments_2024_25_243.xlsx")
    # --- a different, later schema: other header names, estimated + approved amount, payment date column
    alt = ["Sl No", "Work ID", "Work Description", "Ward Number", "Department", "Contractor Name", "Estimated Amount", "Approved Amount", "Bill Number",
           "Payment Amount", "Payment Date", "Work Order Date", "Financial Year"]
    ws2 = openpyxl.Workbook().active
    with open(OUT / "alt_schema_2025_26.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(alt)
        w.writerow([1, "WO/ALT/001", "Resurfacing of 5th Main Road, Indiranagar", "80", "Roads", "M/s Alt Constructions Pvt Ltd", "1,200,000", "1,150,000", "B-17", "1,000,000", "15/07/2025", "02/04/2025", "2025-26"])
        w.writerow([2, "WO/ALT/002", "Pothole filling on 100 Feet Road", "80", "Roads", "M/s Alt Constructions Pvt Ltd", "300000", "280000", "B-18", "", "", "10/04/2025", "2025-26"])


def ckan_page(slug, title, notes, resources, meta, license_text="Other (Public Domain)", next_url=None):
    """Reconstruction of a CKAN dataset page (the markup CKAN's default theme produces). resources: [(id, uuid_dataset, name, description, filename)]"""
    items = []
    for rid, ds, name, desc, fname in resources:
        items.append(f'''
        <li class="resource-item" data-id="{rid}">
          <a class="heading" href="/dataset/{slug}/resource/{rid}" title="{name}">{name}<span class="format-label" property="dc:format" data-format="csv">CSV</span></a>
          <p class="description">{desc}</p>
          <div class="dropdown btn-group">
            <a href="#" class="btn btn-primary dropdown-toggle" data-toggle="dropdown">Explore <span class="caret"></span></a>
            <ul class="dropdown-menu">
              <li><a href="/dataset/{slug}/resource/{rid}"><i class="fa fa-bar-chart-o"></i> Preview</a></li>
              <li><a href="https://data.opencity.in/dataset/{ds}/resource/{rid}/download/{fname}" class="resource-url-analytics" target="_blank"><i class="fa fa-arrow-circle-o-down"></i> Download</a></li>
            </ul>
          </div>
        </li>''')
    rows = "".join(f"<tr><th scope='row'>{k}</th><td>{v}</td></tr>" for k, v in meta.items())
    nxt = f'<ul class="pagination"><li class="next"><a rel="next" href="{next_url}">Next</a></li></ul>' if next_url else ""
    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>{title} - Dataset - OpenCity</title>
<meta name="description" content="{notes}"></head><body>
<div class="toolbar"><ol class="breadcrumb"><li><a href="/dataset/">Datasets</a></li><li class="active"><a href="/dataset/{slug}">{title}</a></li></ol></div>
<article class="module"><h1>{title}</h1><div class="notes embedded-content"><p>{notes}</p></div></article>
<section id="dataset-resources" class="resources"><h3>Data and Resources</h3><ul class="resource-list">{"".join(items)}</ul>{nxt}</section>
<section class="additional-info"><h3>Additional Info</h3><table class="table table-striped table-bordered table-condensed"><thead><tr><th>Field</th><th>Value</th></tr></thead><tbody>{rows}</tbody></table></section>
<section id="dataset-license" class="module module-narrow module-shallow"><h2 class="module-heading">License</h2><p class="module-content">{license_text}</p></section>
</body></html>'''


# the resource ids / dataset ids / file names below are the ones listed on the live pages; the files behind them in the tests are the synthetic ones above
P24 = [("67637545-30aa-4a6c-80fa-b46bc22bdc24", "4e539082-aca3-4df0-b676-dc1655cf17d2", "BBMP Work Orders and Payments for 198 Wards Regime", "Work orders and payments for 2024-25 for the 198-ward regime.", "5cfd015d-cc0b-4526-b6e2-e547e740345a.csv"),
       ("f0606cb1-51f7-48f8-8c38-8995f790a958", "4e539082-aca3-4df0-b676-dc1655cf17d2", "BBMP Work Orders and Payments for 225 Wards Regime", "Work orders and payments for 2024-25 for the 225-ward regime.", "c57cb808-16cc-4dc7-87e1-4ec55ae58dd3.csv"),
       ("83d27234-f87c-4a0b-a668-a58bd57b5034", "4e539082-aca3-4df0-b676-dc1655cf17d2", "BBMP Work Orders and Payments for 243 Wards Regime", "Work orders and payments for 2024-25 for the 243-ward regime.", "7b43174d-fa07-46f5-b27b-e7b6ccb1a4bd.csv")]
P23 = [("375708e4-be2e-4c35-bc12-edbacedc2ab2", "356bfc9c-6abe-4d16-b719-a90e265c2e28", "BBMP Work Orders 2023-24", "Work orders for BBMP. The ward numbers are still the old ward numbers 1-198.", "3cd0f9a1-a899-46a8-81e4-b79834ad8408.csv")]
P25 = [("7a6907c2-6efd-43d6-99ac-1d10f67b3f85", "280a1196-074e-43ac-acd9-914918b429f5", "Work Orders and Payments for 198 Wards Regime", "", "bbmp-2025-26-198-wards-work-orders.csv"),
       ("6c2e5dfa-845c-440d-808c-3ed45b083059", "280a1196-074e-43ac-acd9-914918b429f5", "Work Orders and Payments for 243 Wards Regime", "The 243 wards regime was active from 2022 to 2023.", "bbmp-2025-26-243-wards-work-orders.csv"),
       ("3e784947-b321-4cba-bedb-69de53450a83", "280a1196-074e-43ac-acd9-914918b429f5", "Work Orders and Payments for 225 Wards Regime", "The 225 wards regime is active since 2023.", "bbmp-2025-26-225-wards-work-orders.csv"),
       ("71b93422-3d64-4de6-a823-468517f441e3", "280a1196-074e-43ac-acd9-914918b429f5", "Work Orders and Payments for Common Wards", "Common wards have numbers 300 and upwards.", "bbmp-2025-26-common-wards-work-orders.csv")]


def make_html():
    (OUT / "dataset_2024_25.html").write_text(ckan_page("bbmp-work-orders-and-payments-2024-25", "BBMP Work Orders and Payments 2024-25", "Work orders and payments for the 2024-25 fiscal year.", P24,
        {"Source": "https://account.bbmpgov.in", "Author": "Vaidya R", "Last Updated": "November 27, 2025, 12:38 (UTC)", "Created": "July 14, 2025, 06:04 (UTC)"}), encoding="utf-8")
    (OUT / "dataset_2023_24.html").write_text(ckan_page("bbmp-work-orders-2023-24", "BBMP Work Orders (2023-24)", "Work orders of BBMP for 2023-24", P23,
        {"Source": "https://site.bbmp.gov.in", "Author": "Vaidyanathan R", "Last Updated": "November 27, 2025, 12:38 (UTC)", "Created": "May 29, 2024, 09:58 (UTC)"}), encoding="utf-8")
    (OUT / "dataset_2025_26.html").write_text(ckan_page("bbmp-work-orders-and-payments-2025-26", "BBMP Work Orders and Payments 2025-26", "Work orders and payments done in the year 2025-26", P25,
        {"Source": "accounts.bbmp.gov.in", "Author": "Vaidya R", "Last Updated": "May 14, 2026, 09:42 (UTC)", "Created": "May 14, 2026, 09:39 (UTC)"}), encoding="utf-8")
    # a paginated list: page 1 has the first resource and a Next link, page 2 the other
    (OUT / "dataset_paginated_p1.html").write_text(ckan_page("bbmp-work-orders-and-payments-2024-25", "BBMP Work Orders and Payments 2024-25", "Paginated", P24[:1], {"Author": "x"},
        next_url="/dataset/bbmp-work-orders-and-payments-2024-25?page=2"), encoding="utf-8")
    (OUT / "dataset_paginated_p2.html").write_text(ckan_page("bbmp-work-orders-and-payments-2024-25", "BBMP Work Orders and Payments 2024-25", "Paginated", P24[1:], {"Author": "x"}), encoding="utf-8")
    # official site: static page with one data link among ordinary links; and a JavaScript-rendered shell with no links at all
    (OUT / "site_bbmp_home.html").write_text('''<!DOCTYPE html><html><head><title>BBMP</title></head><body>
<nav><a href="/about">About</a><a href="/tenders">Tenders</a><a href="https://site.bbmp.gov.in/open-data/bbmp-works-2024.xlsx" title="Works 2024">Works register 2024 (Excel)</a>
<a href="/files/circular.pdf">Circular (PDF)</a><a href="mailto:x@example.org">Mail</a></nav></body></html>''', encoding="utf-8")
    (OUT / "site_js_shell.html").write_text('<!DOCTYPE html><html><head><title>BBMP</title></head><body><div id="app"></div><script src="/app.js"></script></body></html>', encoding="utf-8")
    (OUT / "site_js_rendered.html").write_text('<html><body><a href="https://site.bbmp.gov.in/data/works.csv">Works CSV</a></body></html>', encoding="utf-8")
    (OUT / "login_page.html").write_text('<html><head><title>Sign in</title></head><body><form>Please log in to continue. <div class="g-recaptcha">captcha</div></form></body></html>', encoding="utf-8")


if __name__ == "__main__":
    main()
    make_html()
