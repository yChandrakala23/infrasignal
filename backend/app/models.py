from datetime import datetime
from sqlalchemy import (Column, Integer, String, Float, Boolean, DateTime, Text,
                        ForeignKey, JSON, Index)
from .db import Base

now = datetime.utcnow


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(200), unique=True, nullable=False)
    password_hash = Column(String(300), nullable=False)
    role = Column(String(20), nullable=False)  # citizen, officer, admin
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=now)


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
    geojson = Column(Text)
    __table_args__ = (Index("ix_ward_regime_num", "regime", "number", unique=True),)


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


class SourceRecord(Base):
    __tablename__ = "source_records"
    id = Column(Integer, primary_key=True)
    import_job_id = Column(Integer, ForeignKey("import_jobs.id"))
    row_number = Column(Integer)
    raw = Column(JSON)


class Contractor(Base):
    __tablename__ = "contractors"
    id = Column(Integer, primary_key=True)
    name = Column(String(300))
    name_norm = Column(String(300), index=True)


class Roadwork(Base):
    __tablename__ = "roadworks"
    id = Column(Integer, primary_key=True)
    job_number = Column(String(80), index=True)
    work_order_id = Column(String(80), index=True)
    tender_id = Column(String(120), index=True)
    description = Column(Text)
    description_norm = Column(Text)
    ward_regime = Column(String(10))
    ward_number = Column(String(20))
    ward_name = Column(String(200))
    corporation_id = Column(Integer, ForeignKey("corporations.id"))
    contractor_id = Column(Integer, ForeignKey("contractors.id"))
    sanctioned_amount = Column(Float)
    work_order_date = Column(DateTime)
    source_record_id = Column(Integer, ForeignKey("source_records.id"))
    source_name = Column(String(200))
    source_url = Column(String(500))
    is_demo = Column(Boolean, default=False)
    created_at = Column(DateTime, default=now)


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


class Document(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True)
    roadwork_id = Column(Integer, ForeignKey("roadworks.id"))
    filename = Column(String(300))
    path = Column(String(500))
    doc_type = Column(String(50))  # tender, boq, work_order, bill, completion, other
    pages = Column(Integer)
    sha256 = Column(String(64))
    uploaded_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=now)


class Extraction(Base):
    __tablename__ = "extractions"
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id"), index=True)
    field = Column(String(50))
    raw_value = Column(Text)
    norm_value = Column(Text)
    page = Column(Integer)
    confidence = Column(Float)
    method = Column(String(30))  # text_regex, ocr_regex
    status = Column(String(20), default="unverified")
    corrected_by = Column(Integer, ForeignKey("users.id"))


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
    __table_args__ = (Index("ix_report_latlon", "lat", "lon"),)


class ReportStatus(Base):
    __tablename__ = "report_status"
    id = Column(Integer, primary_key=True)
    report_id = Column(Integer, ForeignKey("reports.id"), index=True)
    status = Column(String(30))
    note_public = Column(Text)
    note_internal = Column(Text)
    by_user = Column(Integer, ForeignKey("users.id"))
    at = Column(DateTime, default=now)


class RecordLink(Base):
    __tablename__ = "record_links"
    id = Column(Integer, primary_key=True)
    a_type = Column(String(30))
    a_id = Column(Integer)
    b_type = Column(String(30))
    b_id = Column(Integer)
    method = Column(String(40))
    confidence = Column(Float)
    evidence = Column(JSON)
    status = Column(String(20), default="suggested")  # suggested, verified, rejected, unverified
    reviewer_id = Column(Integer, ForeignKey("users.id"))
    reviewed_at = Column(DateTime)


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
    status = Column(String(20), default="open")
    dedupe_key = Column(String(120), unique=True)
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
