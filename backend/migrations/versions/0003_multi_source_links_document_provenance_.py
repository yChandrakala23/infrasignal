"""multi source links, document provenance, boq items, page ocr, provisional works

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-12 11:13:27.263421
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('boq_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('document_id', sa.Integer(), nullable=False),
    sa.Column('page', sa.Integer(), nullable=True),
    sa.Column('table_index', sa.Integer(), nullable=True),
    sa.Column('row_index', sa.Integer(), nullable=True),
    sa.Column('row_kind', sa.String(length=12), nullable=True),
    sa.Column('item_no', sa.String(length=40), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('unit', sa.String(length=40), nullable=True),
    sa.Column('quantity', sa.Float(), nullable=True),
    sa.Column('unit_rate', sa.Float(), nullable=True),
    sa.Column('total_amount', sa.Float(), nullable=True),
    sa.Column('calc_total', sa.Float(), nullable=True),
    sa.Column('check_status', sa.String(length=20), nullable=True),
    sa.Column('check_note', sa.String(length=300), nullable=True),
    sa.Column('method', sa.String(length=30), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('raw', sa.JSON(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=True),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('boq_items', schema=None) as batch_op:
        batch_op.create_index('ix_boq_doc_page', ['document_id', 'page'], unique=False)
        batch_op.create_index(batch_op.f('ix_boq_items_document_id'), ['document_id'], unique=False)

    op.create_table('document_pages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('document_id', sa.Integer(), nullable=False),
    sa.Column('page', sa.Integer(), nullable=False),
    sa.Column('method', sa.String(length=24), nullable=True),
    sa.Column('ocr_lang', sa.String(length=20), nullable=True),
    sa.Column('ocr_confidence', sa.Float(), nullable=True),
    sa.Column('dpi', sa.Integer(), nullable=True),
    sa.Column('deskew_deg', sa.Float(), nullable=True),
    sa.Column('preprocess', sa.String(length=80), nullable=True),
    sa.Column('text_chars', sa.Integer(), nullable=True),
    sa.Column('kannada_chars', sa.Integer(), nullable=True),
    sa.Column('needs_review', sa.Boolean(), nullable=True),
    sa.Column('review_reason', sa.String(length=200), nullable=True),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('document_id', 'page', name='uq_document_page')
    )
    with op.batch_alter_table('document_pages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_document_pages_document_id'), ['document_id'], unique=False)

    op.create_table('work_source_links',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('roadwork_id', sa.Integer(), nullable=False),
    sa.Column('source_record_id', sa.Integer(), nullable=False),
    sa.Column('source_type', sa.String(length=30), nullable=True),
    sa.Column('role', sa.String(length=30), nullable=True),
    sa.Column('match_method', sa.String(length=120), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('evidence', sa.JSON(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=True),
    sa.Column('reviewer_id', sa.Integer(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['reviewer_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['roadwork_id'], ['roadworks.id'], ),
    sa.ForeignKeyConstraint(['source_record_id'], ['source_records.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('roadwork_id', 'source_record_id', name='uq_work_source')
    )
    with op.batch_alter_table('work_source_links', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_work_source_links_roadwork_id'), ['roadwork_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_work_source_links_source_record_id'), ['source_record_id'], unique=False)

    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_page_url', sa.String(length=1000), nullable=True))
        batch_op.add_column(sa.Column('download_url', sa.String(length=1000), nullable=True))
        batch_op.add_column(sa.Column('downloaded_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('original_filename', sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column('ocr_summary', sa.JSON(), nullable=True))

    with op.batch_alter_table('payments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('duplicate_of_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_payments_duplicate_of_id'), ['duplicate_of_id'], unique=False)
        batch_op.create_foreign_key('fk_payments_duplicate_of', 'payments', ['duplicate_of_id'], ['id'])

    with op.batch_alter_table('roadworks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('verification_status', sa.String(length=20), server_default='official', nullable=True))
        batch_op.add_column(sa.Column('created_from_document_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('provisional_key', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('merged_into_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_roadworks_provisional_key'), ['provisional_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_roadworks_verification_status'), ['verification_status'], unique=False)
        batch_op.create_foreign_key('fk_roadworks_merged_into', 'roadworks', ['merged_into_id'], ['id'])
        batch_op.create_foreign_key('fk_roadworks_created_from_doc', 'documents', ['created_from_document_id'], ['id'])

    # ------------------------------------------------------------------ backfill: keep every existing record and relationship
    # Plain INSERT ... SELECT so it behaves the same on SQLite and PostgreSQL. Nothing is deleted or changed in the source tables
    # except the two defaults below.
    op.execute("UPDATE roadworks SET verification_status = 'official' WHERE verification_status IS NULL")
    op.execute("UPDATE documents SET original_filename = filename WHERE original_filename IS NULL")
    cols = "(roadwork_id, source_record_id, source_type, role, match_method, confidence, status, created_at)"
    # 1. the pointer the work already had
    op.execute("INSERT INTO work_source_links " + cols + """
        SELECT id, source_record_id, 'works_row', 'legacy_primary', 'original_import', 1.0, 'auto', CURRENT_TIMESTAMP
        FROM roadworks WHERE source_record_id IS NOT NULL""")
    # 2. payment and bill rows that already belonged to a work
    for tbl, stype in (("payments", "payment_row"), ("bills", "bill_row")):
        op.execute("INSERT INTO work_source_links " + cols + f"""
            SELECT DISTINCT x.roadwork_id, x.source_record_id, '{stype}', 'merged_row', 'original_import', 1.0, 'auto', CURRENT_TIMESTAMP
            FROM {tbl} x
            WHERE x.roadwork_id IS NOT NULL AND x.source_record_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM work_source_links l WHERE l.roadwork_id = x.roadwork_id AND l.source_record_id = x.source_record_id)""")
    # 3. tender rows that were already linked to a work (rejected links are not carried over)
    op.execute("INSERT INTO work_source_links (roadwork_id, source_record_id, source_type, role, match_method, confidence, evidence, status, created_at) "
               """SELECT rl.a_id, t.source_record_id, 'tender_row', 'tender', rl.method, rl.confidence, rl.evidence, rl.status, CURRENT_TIMESTAMP
                  FROM record_links rl JOIN tenders t ON t.id = rl.b_id
                  WHERE rl.a_type = 'roadwork' AND rl.b_type = 'tender' AND rl.status IN ('auto', 'verified', 'suggested')
                    AND t.source_record_id IS NOT NULL
                    AND EXISTS (SELECT 1 FROM roadworks w WHERE w.id = rl.a_id)
                    AND NOT EXISTS (SELECT 1 FROM work_source_links l WHERE l.roadwork_id = rl.a_id AND l.source_record_id = t.source_record_id)""")


def downgrade():
    with op.batch_alter_table('roadworks', schema=None) as batch_op:
        batch_op.drop_constraint('fk_roadworks_created_from_doc', type_='foreignkey')
        batch_op.drop_constraint('fk_roadworks_merged_into', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_roadworks_verification_status'))
        batch_op.drop_index(batch_op.f('ix_roadworks_provisional_key'))
        batch_op.drop_column('merged_into_id')
        batch_op.drop_column('provisional_key')
        batch_op.drop_column('created_from_document_id')
        batch_op.drop_column('verification_status')

    with op.batch_alter_table('payments', schema=None) as batch_op:
        batch_op.drop_constraint('fk_payments_duplicate_of', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_payments_duplicate_of_id'))
        batch_op.drop_column('duplicate_of_id')

    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_column('ocr_summary')
        batch_op.drop_column('original_filename')
        batch_op.drop_column('downloaded_at')
        batch_op.drop_column('download_url')
        batch_op.drop_column('source_page_url')

    with op.batch_alter_table('work_source_links', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_work_source_links_source_record_id'))
        batch_op.drop_index(batch_op.f('ix_work_source_links_roadwork_id'))
    op.drop_table('work_source_links')
    with op.batch_alter_table('document_pages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_document_pages_document_id'))
    op.drop_table('document_pages')
    with op.batch_alter_table('boq_items', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_boq_items_document_id'))
        batch_op.drop_index('ix_boq_doc_page')
    op.drop_table('boq_items')
