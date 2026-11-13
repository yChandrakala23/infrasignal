"""Shared helpers for the BBMP collection tests: a pretend website built from saved fixtures (no network), and a way to wipe collected data
between tests (the test database is shared by the whole session)."""
import hashlib
from collections import Counter
from pathlib import Path

import httpx

from app.connectors.bbmp_connector import Fetcher

FIX = Path(__file__).parent / "fixtures" / "bbmp"
DS = "https://data.opencity.in"
SLUG24, SLUG23, SLUG25 = "bbmp-work-orders-and-payments-2024-25", "bbmp-work-orders-2023-24", "bbmp-work-orders-and-payments-2025-26"
# (resource id, dataset uuid, file name) as listed on the live dataset pages
R24 = {"198": ("67637545-30aa-4a6c-80fa-b46bc22bdc24", "4e539082-aca3-4df0-b676-dc1655cf17d2", "5cfd015d-cc0b-4526-b6e2-e547e740345a.csv"),
       "225": ("f0606cb1-51f7-48f8-8c38-8995f790a958", "4e539082-aca3-4df0-b676-dc1655cf17d2", "c57cb808-16cc-4dc7-87e1-4ec55ae58dd3.csv"),
       "243": ("83d27234-f87c-4a0b-a668-a58bd57b5034", "4e539082-aca3-4df0-b676-dc1655cf17d2", "7b43174d-fa07-46f5-b27b-e7b6ccb1a4bd.csv")}
R23 = {"198": ("375708e4-be2e-4c35-bc12-edbacedc2ab2", "356bfc9c-6abe-4d16-b719-a90e265c2e28", "3cd0f9a1-a899-46a8-81e4-b79834ad8408.csv")}
R25 = {"198": ("7a6907c2-6efd-43d6-99ac-1d10f67b3f85", "280a1196-074e-43ac-acd9-914918b429f5", "bbmp-2025-26-198-wards-work-orders.csv"),
       "243": ("6c2e5dfa-845c-440d-808c-3ed45b083059", "280a1196-074e-43ac-acd9-914918b429f5", "bbmp-2025-26-243-wards-work-orders.csv"),
       "225": ("3e784947-b321-4cba-bedb-69de53450a83", "280a1196-074e-43ac-acd9-914918b429f5", "bbmp-2025-26-225-wards-work-orders.csv"),
       "common": ("71b93422-3d64-4de6-a823-468517f441e3", "280a1196-074e-43ac-acd9-914918b429f5", "bbmp-2025-26-common-wards-work-orders.csv")}


def dl_url(res):
    rid, ds, fname = res
    return f"{DS}/dataset/{ds}/resource/{rid}/download/{fname}"


def fixture(name) -> bytes:
    return (FIX / name).read_bytes()


class Site:
    """A website made of canned responses. Counts requests; can answer conditional requests with 304."""

    def __init__(self, etags=True):
        self.routes, self.hits, self.etags, self.requests = {}, Counter(), etags, []
        self.fail_next = Counter()  # url -> number of 503 answers before succeeding

    def add(self, url, body, status=200, ctype=None, headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        ctype = ctype or ("text/html; charset=utf-8" if body[:1] == b"<" else "text/csv")
        self.routes[url] = {"status": status, "body": body, "ctype": ctype, "headers": headers or {}}

    def remove(self, url):
        self.routes.pop(url, None)

    def handler(self, request: httpx.Request):
        url = str(request.url)
        self.hits[url] += 1
        self.requests.append((url, dict(request.headers)))
        if self.fail_next[url] > 0:
            self.fail_next[url] -= 1
            return httpx.Response(503, text="busy")
        r = self.routes.get(url)
        if r is None:
            return httpx.Response(404, text="not found")
        etag = '"' + hashlib.md5(r["body"]).hexdigest() + '"' if self.etags and r["status"] == 200 and not url.endswith("robots.txt") else None
        if etag and request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers={"etag": etag})
        h = {"content-type": r["ctype"], **r["headers"]}
        if etag:
            h["etag"] = etag
        return httpx.Response(r["status"], content=r["body"], headers=h)

    def fetcher(self, **kw):
        kw.setdefault("delay", 0)
        kw.setdefault("sleep", lambda s: None)
        kw.setdefault("backoff", 0)
        return Fetcher(client=httpx.Client(transport=httpx.MockTransport(self.handler), follow_redirects=True), **kw)

    # -- the standard BBMP layout
    def standard(self, only=("24", "23", "25"), with_225_2024=False):
        if "24" in only:
            self.add(f"{DS}/dataset/{SLUG24}", fixture("dataset_2024_25.html"))
            self.add(dl_url(R24["243"]), fixture("wo_payments_2024_25_243.csv"))
            self.add(dl_url(R24["198"]), fixture("wo_payments_2024_25_198.csv"))
            if with_225_2024:
                self.add(dl_url(R24["225"]), fixture("wo_payments_2025_26_225.csv"))
        if "23" in only:
            self.add(f"{DS}/dataset/{SLUG23}", fixture("dataset_2023_24.html"))
            self.add(dl_url(R23["198"]), fixture("wo_2023_24_198.csv"))
        if "25" in only:
            self.add(f"{DS}/dataset/{SLUG25}", fixture("dataset_2025_26.html"))
            for k, name in (("198", "wo_payments_2025_26_198.csv"), ("243", "wo_payments_2025_26_243.csv"), ("225", "wo_payments_2025_26_225.csv"),
                            ("common", "wo_payments_2025_26_common.csv")):
                self.add(dl_url(R25[k]), fixture(name))
        return self


def only_config(*datasets, site_pages=()):
    """A sources config naming just the given dataset slugs (and no official-site scan unless asked)."""
    names = {SLUG24: "2024-25", SLUG23: "2023-24", SLUG25: "2025-26"}
    return {"source_website": DS, "datasets": [{"slug": s, "url": f"{DS}/dataset/{s}", "fiscal_year": names[s], "enabled": True,
                                                **({"default_ward_regime": "198"} if s == SLUG23 else {})} for s in datasets],
            "site_pages": [{"url": u, "enabled": True} for u in site_pages]}


def wipe(db):
    """Remove everything the BBMP pipeline stored, leaving all other test data alone."""
    from app import models as m
    db.rollback()
    work_ids = [i for (i,) in db.query(m.Roadwork.id).filter((m.Roadwork.data_origin == "bbmp_open_data") | m.Roadwork.external_key.isnot(None))]
    src_ids = [i for (i,) in db.query(m.SourceRecord.id).filter(m.SourceRecord.external_id.isnot(None))]
    db.query(m.ImportIssue).delete()
    db.query(m.Payment).filter(m.Payment.external_key.isnot(None)).update({m.Payment.duplicate_of_id: None})
    db.query(m.Payment).filter(m.Payment.external_key.isnot(None)).delete()
    db.query(m.Bill).filter(m.Bill.external_key.isnot(None)).delete()
    if work_ids:
        db.query(m.Payment).filter(m.Payment.roadwork_id.in_(work_ids)).update({m.Payment.duplicate_of_id: None})
        db.query(m.Payment).filter(m.Payment.roadwork_id.in_(work_ids)).delete()
        db.query(m.Bill).filter(m.Bill.roadwork_id.in_(work_ids)).delete()
        db.query(m.WorkSourceLink).filter(m.WorkSourceLink.roadwork_id.in_(work_ids)).delete()
        db.query(m.Roadwork).filter(m.Roadwork.id.in_(work_ids)).delete()
    if src_ids:
        db.query(m.WorkSourceLink).filter(m.WorkSourceLink.source_record_id.in_(src_ids)).delete()
        db.query(m.SourceRecord).filter(m.SourceRecord.id.in_(src_ids)).delete()
    db.query(m.SourceFile).update({m.SourceFile.import_job_id: None})
    db.query(m.ImportJob).filter(m.ImportJob.kind == "bbmp_open_data").delete()
    db.query(m.SourceFile).delete()
    db.query(m.CollectionRun).delete()
    db.query(m.Contractor).filter(m.Contractor.is_demo.is_(False), ~m.Contractor.id.in_(db.query(m.Roadwork.contractor_id).filter(m.Roadwork.contractor_id.isnot(None))),
                                                    ~m.Contractor.id.in_(db.query(m.Tender.contractor_id).filter(m.Tender.contractor_id.isnot(None)))).delete(synchronize_session=False)
    db.commit()


def Q(db, model):
    """Query a table limited to what the BBMP pipeline stored, so tests do not depend on sample/other data that earlier tests left in the shared database."""
    from app import models as m
    q = db.query(model)
    if model is m.Roadwork:
        return q.filter(m.Roadwork.external_key.isnot(None))
    if model is m.SourceRecord:
        return q.filter(m.SourceRecord.external_id.isnot(None))
    if model in (m.Payment, m.Bill):
        return q.filter(model.external_key.isnot(None))
    if model is m.Contractor:
        return q.filter(m.Contractor.id.in_(db.query(m.Roadwork.contractor_id).filter(m.Roadwork.external_key.isnot(None), m.Roadwork.contractor_id.isnot(None))))
    if model is m.WorkSourceLink:
        return q.filter(m.WorkSourceLink.source_record_id.in_(db.query(m.SourceRecord.id).filter(m.SourceRecord.external_id.isnot(None))))
    return q
