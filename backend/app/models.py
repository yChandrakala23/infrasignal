"""Database models.

Everything in the original prototype schema is kept. New columns are nullable so an existing
database upgrades in place (see migrations/). Geometry is stored portably as GeoJSON text plus a
bounding box; on PostgreSQL the migration additionally creates real PostGIS `geom` columns with
GiST indexes (see app/spatial.py), which are filled by `spatial.sync_postgis()`.
"""
from datetime import datetime, timezone
from sqlalchemy import (Column, Integer, String, Float, Boolean, DateTime, Text,
                        ForeignKey, JSON, Index, UniqueConstraint)
from .db import Base


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- users / audit
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(200), unique=True, nullable=False)
    password_hash = Column(String(300), nullable=False)
    role = Column(String(20), nullable=False)  # officer (gov monitoring), authority (complaint handling), admin
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=now)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    action = Column(String(60))
    entity = Column(String(40))
    entity_id = Column(Integer)
    detail = Column(JSON)
    at = Column(DateTime, default=now)


# --------------------------------------------------------------------------- geography
class Corporation(Base):
    __tablename__ = "corporations"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True)


class Ward(Base):
    __tablename__ = "wards"
    id = Column(Integer, primary_key=True)
    regime = Column(String(10), nullable=False)  # 198, 243, 225, gba369
    number = Column(String(20), nullable=False)
    name = Column(String(200))
    corporation_id = Column(Integer, ForeignKey("corporations.id"))
    geojson = Column(Text)  # geometry only
    source = Column(String(200))
    props = Column(JSON)
    centroid_lat = Column(Float)
    centroid_lon = Column(Float)
    minx = Column(Float); miny = Column(Float); maxx = Column(Float); maxy = Column(Float)
    __table_args__ = (Index("ix_ward_regime_num", "regime", "number", unique=True),)


class Road(Base):
    """A named (or unnamed) road. The unit that citizens and officers read about."""
    __tablename__ = "roads"
    id = Column(Integer, primary_key=True)
    name = Column(String(300), index=True)  # NULL for unnamed ways: they are still mapped and reportable
    name_norm = Column(String(300), index=True)
    road_class = Column(String(40))
    source = Column(String(120))  # SAMPLE_APPROX, OSM, BBMP_CENTERLINE ...
    source_ref = Column(String(120))  # e.g. OSM way id
    is_approx = Column(Boolean, default=False)
    is_demo = Column(Boolean, default=False)
    length_m = Column(Float)
    centroid_lat = Column(Float)
    centroid_lon = Column(Float)
    created_at = Column(DateTime, default=now)


class RoadSegment(Base):
    """A piece of a road (<= ~400 m and inside one ward). Reports attach to segments."""
    __tablename__ = "road_segments"
    id = Column(Integer, primary_key=True)
    road_id = Column(Integer, ForeignKey("roads.id"), index=True, nullable=False)
    seq = Column(Integer)
    ward_id = Column(Integer, ForeignKey("wards.id"), index=True)
    ward_regime = Column(String(10))
    ward_number = Column(String(20))
    ward_name = Column(String(200))
    length_m = Column(Float)
    geojson = Column(Text, nullable=False)
    mid_lat = Column(Float)
    mid_lon = Column(Float)
    minx = Column(Float); miny = Column(Float); maxx = Column(Float); maxy = Column(Float)
    __table_args__ = (Index("ix_seg_bbox", "minx", "maxx", "miny", "maxy"),)


# --------------------------------------------------------------------------- imports / provenance
class ImportJob(Base):
    __tablename__ = "import_jobs"
    id = Column(Integer, primary_key=True)
    filename = Column(String(300))
    kind = Column(String(20))
    source_name = Column(String(200))
    source_url = Column(String(500))
    ward_regime = Column(String(10))
    status = Column(String(20), default="running")
    rows_total = Column(Integer, default=0)
    rows_ok = Column(Integer, default=0)
    rows_skipped = Column(Integer, default=0)
    rows_failed = Column(Integer, default=0)
    column_map = Column(JSON)
    errors = Column(JSON)
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=now)
    fiscal_year = Column(String(10))
    is_demo = Column(Boolean, default=False)
    rows_duplicate = Column(Integer, default=0)
    stats = Column(JSON)


class SourceRecord(Base):
    __tablename__ = "source_records"
    id = Column(Integer, primary_key=True)
    import_job_id = Column(Integer, ForeignKey("import_jobs.id"))
    row_number = Column(Integer)
    raw = Column(JSON)
    row_hash = Column(String(64), index=True)  # duplicate detection


# --------------------------------------------------------------------------- government records
class Contractor(Base):
    __tablename__ = "contractors"
    id = Column(Integer, primary_key=True)
    name = Column(String(300))
    name_norm = Column(String(300), index=True)
    is_demo = Column(Boolean, default=False)


class Roadwork(Base):
    __tablename__ = "roadworks"
    id = Column(Integer, primary_key=True)
    job_number = Column(String(80), index=True)  # BBMP job number
    work_order_id = Column(String(80), index=True)
    tender_id = Column(String(120), index=True)
    description = Column(Text)
    description_norm = Column(Text)
    ward_regime = Column(String(10))
    ward_number = Column(String(20))
    ward_name = Column(String(200))
    corporation_id = Column(Integer, ForeignKey("corporations.id"))
    contractor_id = Column(Integer, ForeignKey("contractors.id"))
    sanctioned_amount = Column(Float)  # amount as published in the source (work order / sanction amount)
    work_order_date = Column(DateTime)
    source_record_id = Column(Integer, ForeignKey("source_records.id"))
    source_name = Column(String(200))
    source_url = Column(String(500))
    is_demo = Column(Boolean, default=False)
    created_at = Column(DateTime, default=now)
    # --- added for the government monitoring module
    work_id = Column(String(120), index=True)  # contract / agreement reference
    department = Column(String(200), index=True)
    category = Column(String(40), index=True)  # resurfacing, pothole_repair, white_topping, dug_road_restoration, road_other
    estimated_cost = Column(Float)
    awarded_amount = Column(Float)
    commencement_date = Column(DateTime)
    completion_deadline = Column(DateTime)
    # Declared status, only ever taken from a record that states it. Never inferred from payments.
    work_status = Column(String(30), default="not_stated", index=True)
    work_status_source = Column(String(200))
    fiscal_year = Column(String(10), index=True)
    location_text = Column(String(300))
    road_name_extracted = Column(String(300))
    ward_is_multi = Column(Boolean, default=False)  # BBMP ward numbers > 300 denote multi-ward / non-geographic works
    data_origin = Column(String(20), default="import")  # import, document, manual


class Tender(Base):
    __tablename__ = "tenders"
    id = Column(Integer, primary_key=True)
    tender_id = Column(String(120), index=True)
    title = Column(Text)
    estimated_value = Column(Float)
    awarded_value = Column(Float)
    status = Column(String(50))
    url = Column(String(500))
    source_record_id = Column(Integer, ForeignKey("source_records.id"))
    # --- added
    job_number = Column(String(80), index=True)
    department = Column(String(200))
    category = Column(String(40))
    published_date = Column(DateTime)
    closing_date = Column(DateTime)
    award_date = Column(DateTime)
    contractor_id = Column(Integer, ForeignKey("contractors.id"))
    location_text = Column(String(300))
    source_name = Column(String(200))
    is_demo = Column(Boolean, default=False)
    document_id = Column(Integer, ForeignKey("documents.id"))  # NIT this record was derived from, if any
    created_at = Column(DateTime, default=now)


class Bill(Base):
    __tablename__ = "bills"
    id = Column(Integer, primary_key=True)
    roadwork_id = Column(Integer, ForeignKey("roadworks.id"), index=True)
    bill_number = Column(String(80))
    amount = Column(Float)
    bill_date = Column(DateTime)
    stage = Column(String(80))
    source_record_id = Column(Integer, ForeignKey("source_records.id"))


class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    roadwork_id = Column(Integer, ForeignKey("roadworks.id"), index=True)
    bill_id = Column(Integer, ForeignKey("bills.id"))
    amount = Column(Float)
    paid_on = Column(DateTime)
    reference = Column(String(120))
    source_record_id = Column(Integer, ForeignKey("source_records.id"))
    document_id = Column(Integer, ForeignKey("documents.id"))


class Document(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True)
    roadwork_id = Column(Integer, ForeignKey("roadworks.id"))  # set only for exact-ID matches or after officer verification
    filename = Column(String(300))
    path = Column(String(500))
    doc_type = Column(String(50))  # nit, boq, admin_approval, technical_sanction, loa, work_order, agreement, measurement_book, payment_record, completion_certificate, other
    pages = Column(Integer)
    sha256 = Column(String(64), index=True)
    uploaded_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=now)
    # --- added
    source_name = Column(String(100))  # BBMP, KPPP, GBA, PAIMANA, other
    doc_type_confidence = Column(Float)
    classification = Column(JSON)  # scores and the cue phrases that decided the type
    status = Column(String(20), default="processed")  # processed, needs_review, failed
    match_status = Column(String(20), default="unmatched")  # unmatched, auto, needs_review, verified, manual
    doc_date = Column(DateTime)
    text_chars = Column(Integer)
    ocr_pages = Column(Integer, default=0)
    is_demo = Column(Boolean, default=False)
    notes = Column(Text)


class Extraction(Base):
    __tablename__ = "extractions"
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id"), index=True)
    field = Column(String(50))
    raw_value = Column(Text)
    norm_value = Column(Text)
    page = Column(Integer)
    confidence = Column(Float)
    method = Column(String(30))  # text_regex, ocr_regex, llm
    status = Column(String(20), default="unverified")  # unverified, verified, corrected, rejected
    corrected_by = Column(Integer, ForeignKey("users.id"))
    # --- added: provenance
    snippet = Column(Text)  # surrounding text so a reviewer can see where the value came from
    bbox = Column(JSON)  # [x0, y0, x1, y1] on the cited page (PDF points), when it could be located


# --------------------------------------------------------------------------- matching
class MatchRun(Base):
    __tablename__ = "match_runs"
    id = Column(Integer, primary_key=True)
    kind = Column(String(30))  # documents, tenders, roads, complaints
    params = Column(JSON)
    stats = Column(JSON)
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=now)


class RecordLink(Base):
    __tablename__ = "record_links"
    id = Column(Integer, primary_key=True)
    a_type = Column(String(30))
    a_id = Column(Integer)
    b_type = Column(String(30))
    b_id = Column(Integer)
    method = Column(String(160))
    confidence = Column(Float)
    evidence = Column(JSON)
    status = Column(String(20), default="suggested")  # auto, suggested, verified, rejected, unverified
    reviewer_id = Column(Integer, ForeignKey("users.id"))
    reviewed_at = Column(DateTime)
    run_id = Column(Integer, ForeignKey("match_runs.id"))
    created_at = Column(DateTime, default=now)
    __table_args__ = (Index("ix_link_a", "a_type", "a_id"), Index("ix_link_b", "b_type", "b_id"))


class MatchEvent(Base):
    """Append-only history of every matching decision (machine or human)."""
    __tablename__ = "match_events"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("match_runs.id"))
    link_id = Column(Integer, ForeignKey("record_links.id"))
    subject_type = Column(String(30))
    subject_id = Column(Integer, index=True)
    action = Column(String(30))  # auto_linked, suggested, conflict, unmatched, verified, rejected, reopened
    detail = Column(JSON)
    by_user = Column(Integer, ForeignKey("users.id"))
    at = Column(DateTime, default=now)


# --------------------------------------------------------------------------- citizen reports
class Cluster(Base):
    __tablename__ = "clusters"
    id = Column(Integer, primary_key=True)
    category = Column(String(40))
    lat = Column(Float)
    lon = Column(Float)
    report_count = Column(Integer)
    first_at = Column(DateTime)
    last_at = Column(DateTime)
    status = Column(String(20), default="auto")  # auto, verified, rejected
    public_status = Column(String(40), default="received")
    evidence = Column(JSON)
    note_internal = Column(Text)
    # --- added
    road_id = Column(Integer, ForeignKey("roads.id"), index=True)
    segment_id = Column(Integer, ForeignKey("road_segments.id"))
    ward_label = Column(String(120))
    severity = Column(String(10))
    summary = Column(Text)
    gov_match_status = Column(String(20))  # matched, possible, unmatched


class Report(Base):
    __tablename__ = "reports"
    id = Column(Integer, primary_key=True)
    public_ref = Column(String(20), unique=True, index=True)
    token_hash = Column(String(64))
    category = Column(String(40))
    description = Column(Text)
    lat = Column(Float)
    lon = Column(Float)
    accuracy_m = Column(Float)
    road_name = Column(String(200))
    photo_path = Column(String(500))
    contact = Column(String(200))  # private, never returned by public endpoints
    status = Column(String(30), default="received")
    cluster_id = Column(Integer, ForeignKey("clusters.id"), nullable=True)
    ward_label = Column(String(100))
    client_hash = Column(String(64))
    is_demo = Column(Boolean, default=False)
    is_duplicate = Column(Boolean, default=False)
    created_at = Column(DateTime, default=now, index=True)
    # --- added
    road_id = Column(Integer, ForeignKey("roads.id"), index=True)
    segment_id = Column(Integer, ForeignKey("road_segments.id"), index=True)
    road_distance_m = Column(Float)
    road_match_method = Column(String(30))  # nearest_segment, none
    ward_id = Column(Integer, ForeignKey("wards.id"))
    photo_hash = Column(String(32))
    gov_match_status = Column(String(20), default="unmatched")  # matched, possible, unmatched
    __table_args__ = (Index("ix_report_latlon", "lat", "lon"),)


class ReportPhoto(Base):
    __tablename__ = "report_photos"
    id = Column(Integer, primary_key=True)
    report_id = Column(Integer, ForeignKey("reports.id"), index=True)
    path = Column(String(500))
    phash = Column(String(32))
    created_at = Column(DateTime, default=now)


class ReportStatus(Base):
    __tablename__ = "report_status"
    id = Column(Integer, primary_key=True)
    report_id = Column(Integer, ForeignKey("reports.id"), index=True)
    status = Column(String(30))
    note_public = Column(Text)
    note_internal = Column(Text)
    by_user = Column(Integer, ForeignKey("users.id"))
    at = Column(DateTime, default=now)


# --------------------------------------------------------------------------- alerts
class Alert(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True)
    rule = Column(String(40))
    severity = Column(String(10))
    title = Column(String(300))
    explanation = Column(Text)
    evidence = Column(JSON)
    cluster_id = Column(Integer, ForeignKey("clusters.id"))
    roadwork_id = Column(Integer, ForeignKey("roadworks.id"))
    status = Column(String(20), default="open")  # open, acknowledged, dismissed, resolved, auto_closed
    dedupe_key = Column(String(120), unique=True)
    created_at = Column(DateTime, default=now)
    # --- added
    road_id = Column(Integer, ForeignKey("roads.id"))
    ward_label = Column(String(120))
    updated_at = Column(DateTime)
    updated_by = Column(Integer, ForeignKey("users.id"))
    resolution_note = Column(Text)


class AlertSetting(Base):
    """Configurable thresholds. One row per rule; the engine falls back to defaults in app/alerts.py."""
    __tablename__ = "alert_settings"
    id = Column(Integer, primary_key=True)
    rule = Column(String(40), unique=True, nullable=False)
    enabled = Column(Boolean, default=True)
    params = Column(JSON)
    updated_by = Column(Integer, ForeignKey("users.id"))
    updated_at = Column(DateTime, default=now)
