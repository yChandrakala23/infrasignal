"""BBMP IFMS "Works Bill Public View" (https://accounts.bbmp.gov.in/PublicView/).

How the portal exposes data (read from the page's own script on 12-13 Nov 2026):
  * a public search box (work name, ward, division or contractor); no login and no CAPTCHA;
  * the result grid is filled by a JSON service:
        GET vss00CvStatusData.php?pAction=LoadPaymentGridData&pCriteria=<text>&pDateFrom=01-Jan-1900&pDateTo=01-Jan-1900
            &pDateType=pDF&pOrderBy=-1&pFinancialYearID=-1&pBudgetHeadID=-1&pDDOIDs=-1&pWardIDs=&pDateFilterYN=false&pWardID=-1
    returning rows {wbid, slno, wcname, jobcode, nameofwork, brdetails, amount, remarks};
  * per-bill details: pAction=LoadWorksbillDetails&pWorkBillID=<wbid>.

Status when this was written: the service answered HTTP 500 with an empty body to every request, including the requests the page makes
itself when it loads, so no row could be read. The collector reports that as a source failure; it never invents rows.
"""
import re
from urllib.parse import urlencode

from .bbmp_connector import Fetcher, FetchError

GRID_DEFAULTS = {"pDateFrom": "01-Jan-1900", "pDateTo": "01-Jan-1900", "pDateType": "pDF", "pOrderBy": "-1", "pFinancialYearID": "-1",
                 "pBudgetHeadID": "-1", "pDDOIDs": "-1", "pWardIDs": "", "pDateFilterYN": "false", "pWardID": "-1"}
JOB_CODE = re.compile(r"\b(\d{3})-(\d{2})-(\d{6})\b")
BR = re.compile(r"BR\s*-\s*(\d+)\s*/\s*([0-9]{1,2}-[A-Za-z]{3}-[0-9]{4})", re.I)
RTGS = re.compile(r"Rtgs\s*-\s*(\d+)\s*/\s*([0-9]{1,2}-[A-Za-z]{3}-[0-9]{4})", re.I)


class IfmsClient:
    def __init__(self, fetcher: Fetcher, base_url="https://accounts.bbmp.gov.in/PublicView/"):
        self.f, self.base = fetcher, base_url.rstrip("/") + "/"

    def _json(self, params: dict):
        url = self.base + "vss00CvStatusData.php?" + urlencode(params)
        try:
            r = self.f._request(url)
        except FetchError as e:
            if e.kind == "http" and e.status == 500:
                raise FetchError("http", "HTTP 500 from the IFMS data service (vss00CvStatusData.php): the portal's own server error, "
                                         "not a block. The public page loads but cannot show data either.", 500)
            raise
        text = r.text.strip()
        if not text:
            raise FetchError("not_data", "the IFMS data service returned an empty response")
        try:
            data = r.json()
        except ValueError:
            raise FetchError("not_data", f"the IFMS data service did not return JSON (starts with {text[:60]!r})")
        if isinstance(data, dict) and data.get("rError"):
            raise FetchError("not_data", f"IFMS reported an error: {str(data['rError'])[:200]}")
        return data, url

    def search(self, criteria: str):
        """-> (rows, url). Rows exactly as returned."""
        data, url = self._json({"pAction": "LoadPaymentGridData", "pCriteria": criteria, **GRID_DEFAULTS})
        if not isinstance(data, list):
            raise FetchError("not_data", "unexpected IFMS response shape (expected a list of rows)")
        return data, url

    def bill_details(self, wbid):
        data, _ = self._json({"pAction": "LoadWorksbillDetails", "pWorkBillID": str(wbid)})
        return data


def normalise_ifms_row(row: dict) -> dict:
    """Fields read from one IFMS grid row. Nothing is guessed: missing values stay absent."""
    out = {"category": "payment"}
    job = JOB_CODE.search(str(row.get("jobcode") or "")) or JOB_CODE.search(str(row.get("nameofwork") or ""))
    if job:
        out["job_number"] = job.group(0)
        out["ward_code_in_job"] = job.group(1)
    if row.get("nameofwork"):
        out["description"] = re.sub(r"<[^>]+>", " ", str(row["nameofwork"])).strip()[:500]
    if row.get("wcname"):
        out["ward_or_division"] = str(row["wcname"]).strip()[:200]  # as published; no ward regime is stated, so it is not mapped to a ward
    try:
        if row.get("amount") not in (None, ""):
            out["amount"] = float(str(row["amount"]).replace(",", ""))
    except ValueError:
        pass
    br = str(row.get("brdetails") or "")
    if (m := BR.search(br)):
        out["bill_number"], out["bill_date_text"] = m.group(1), m.group(2)
    if (m := RTGS.search(br)):
        out["payment_reference"], out["payment_date_text"] = m.group(1), m.group(2)
    if row.get("wbid") is not None:
        out["ifms_bill_id"] = str(row["wbid"])
    return out
