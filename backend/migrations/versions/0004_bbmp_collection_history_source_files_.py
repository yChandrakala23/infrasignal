"""BBMP automated collection: collection runs, downloaded source files, import issues, settings, external keys.

All additions are new tables or nullable columns, so an existing database (and its data) upgrades in place.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-12 16:14:10.308796
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('app_settings',
    sa.Column('key', sa.String(length=80), nullable=False),
    sa.Column('value', sa.JSON(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('collection_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('connector', sa.String(length=30), nullable=True),
    sa.Column('trigger', sa.String(length=20), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('requested_by', sa.Integer(), nullable=True),
    sa.Column('params', sa.JSON(), nullable=True),
    sa.Column('datasets_found', sa.Integer(), nullable=True),
    sa.Column('files_found', sa.Integer(), nullable=True),
    sa.Column('files_downloaded', sa.Integer(), nullable=True),
    sa.Column('files_unchanged', sa.Integer(), nullable=True),
    sa.Column('files_failed', sa.Integer(), nullable=True),
    sa.Column('records_processed', sa.Integer(), nullable=True),
    sa.Column('records_inserted', sa.Integer(), nullable=True),
    sa.Column('records_updated', sa.Integer(), nullable=True),
    sa.Column('records_duplicate', sa.Integer(), nullable=True),
    sa.Column('records_failed', sa.Integer(), nullable=True),
    sa.Column('log', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['requested_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('collection_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_collection_runs_status'), ['status'], unique=False)

    op.create_table('source_files',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=True),
    sa.Column('source_website', sa.String(length=200), nullable=True),
    sa.Column('dataset_slug', sa.String(length=200), nullable=True),
    sa.Column('dataset_title', sa.String(length=300), nullable=True),
    sa.Column('dataset_url', sa.String(length=500), nullable=True),
    sa.Column('dataset_meta', sa.JSON(), nullable=True),
    sa.Column('resource_id', sa.String(length=80), nullable=True),
    sa.Column('resource_name', sa.String(length=300), nullable=True),
    sa.Column('download_url', sa.String(length=700), nullable=True),
    sa.Column('original_filename', sa.String(length=300), nullable=True),
    sa.Column('file_format', sa.String(length=10), nullable=True),
    sa.Column('fiscal_year', sa.String(length=10), nullable=True),
    sa.Column('ward_regime', sa.String(length=10), nullable=True),
    sa.Column('size_bytes', sa.Integer(), nullable=True),
    sa.Column('sha256', sa.String(length=64), nullable=True),
    sa.Column('http_etag', sa.String(length=200), nullable=True),
    sa.Column('http_last_modified', sa.String(length=100), nullable=True),
    sa.Column('stored_path', sa.String(length=500), nullable=True),
    sa.Column('downloaded_at', sa.DateTime(), nullable=True),
    sa.Column('last_checked_at', sa.DateTime(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=True),
    sa.Column('import_job_id', sa.Integer(), nullable=True),
    sa.Column('imported_at', sa.DateTime(), nullable=True),
    sa.Column('rows_processed', sa.Integer(), nullable=True),
    sa.Column('rows_inserted', sa.Integer(), nullable=True),
    sa.Column('rows_updated', sa.Integer(), nullable=True),
    sa.Column('rows_duplicate', sa.Integer(), nullable=True),
    sa.Column('rows_failed', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['import_job_id'], ['import_jobs.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['collection_runs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('resource_id', 'sha256', name='uq_source_file_content')
    )
    with op.batch_alter_table('source_files', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_source_files_dataset_slug'), ['dataset_slug'], unique=False)
        batch_op.create_index(batch_op.f('ix_source_files_fiscal_year'), ['fiscal_year'], unique=False)
        batch_op.create_index(batch_op.f('ix_source_files_resource_id'), ['resource_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_source_files_run_id'), ['run_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_source_files_sha256'), ['sha256'], unique=False)
        batch_op.create_index(batch_op.f('ix_source_files_status'), ['status'], unique=False)

    op.create_table('import_issues',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('import_job_id', sa.Integer(), nullable=True),
    sa.Column('source_file_id', sa.Integer(), nullable=True),
    sa.Column('row_number', sa.Integer(), nullable=True),
    sa.Column('kind', sa.String(length=12), nullable=True),
    sa.Column('code', sa.String(length=40), nullable=True),
    sa.Column('message', sa.String(length=400), nullable=True),
    sa.Column('raw', sa.JSON(), nullable=True),
    sa.Column('source_record_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['import_job_id'], ['import_jobs.id'], ),
    sa.ForeignKeyConstraint(['source_file_id'], ['source_files.id'], ),
    sa.ForeignKeyConstraint(['source_record_id'], ['source_records.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('import_issues', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_import_issues_import_job_id'), ['import_job_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_import_issues_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_import_issues_source_file_id'), ['source_file_id'], unique=False)

    with op.batch_alter_table('bills', schema=None) as batch_op:
        batch_op.add_column(sa.Column('deduction', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('net_amount', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('external_key', sa.String(length=300), nullable=True))
        batch_op.create_index('uq_bills_external_key', ['external_key'], unique=True)

    with op.batch_alter_table('contractors', schema=None) as batch_op:
        batch_op.add_column(sa.Column('vendor_code', sa.String(length=20), nullable=True))
        batch_op.create_index(batch_op.f('ix_contractors_vendor_code'), ['vendor_code'], unique=False)

    with op.batch_alter_table('payments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('gross_amount', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('deduction', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('external_key', sa.String(length=300), nullable=True))
        batch_op.create_index('uq_payments_external_key', ['external_key'], unique=True)

    with op.batch_alter_table('roadworks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('external_key', sa.String(length=160), nullable=True))
        batch_op.add_column(sa.Column('verified_fields', sa.JSON(), nullable=True))
        batch_op.create_index('uq_roadworks_external_key', ['external_key'], unique=True)

    with op.batch_alter_table('source_records', schema=None) as batch_op:
        batch_op.add_column(sa.Column('external_id', sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column('source_file_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('fiscal_year', sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column('normalised', sa.JSON(), nullable=True))
        batch_op.create_index(batch_op.f('ix_source_records_fiscal_year'), ['fiscal_year'], unique=False)
        batch_op.create_index(batch_op.f('ix_source_records_source_file_id'), ['source_file_id'], unique=False)
        batch_op.create_index('uq_source_records_external_id', ['external_id'], unique=True)
        batch_op.create_foreign_key('fk_source_records_source_file', 'source_files', ['source_file_id'], ['id'])



def downgrade():
    with op.batch_alter_table('source_records', schema=None) as batch_op:
        batch_op.drop_constraint('fk_source_records_source_file', type_='foreignkey')
        batch_op.drop_index('uq_source_records_external_id')
        batch_op.drop_index(batch_op.f('ix_source_records_source_file_id'))
        batch_op.drop_index(batch_op.f('ix_source_records_fiscal_year'))
        batch_op.drop_column('normalised')
        batch_op.drop_column('fiscal_year')
        batch_op.drop_column('source_file_id')
        batch_op.drop_column('external_id')

    with op.batch_alter_table('roadworks', schema=None) as batch_op:
        batch_op.drop_index('uq_roadworks_external_key')
        batch_op.drop_column('verified_fields')
        batch_op.drop_column('external_key')

    with op.batch_alter_table('payments', schema=None) as batch_op:
        batch_op.drop_index('uq_payments_external_key')
        batch_op.drop_column('external_key')
        batch_op.drop_column('deduction')
        batch_op.drop_column('gross_amount')

    with op.batch_alter_table('contractors', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_contractors_vendor_code'))
        batch_op.drop_column('vendor_code')

    with op.batch_alter_table('bills', schema=None) as batch_op:
        batch_op.drop_index('uq_bills_external_key')
        batch_op.drop_column('external_key')
        batch_op.drop_column('net_amount')
        batch_op.drop_column('deduction')

    with op.batch_alter_table('import_issues', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_import_issues_source_file_id'))
        batch_op.drop_index(batch_op.f('ix_import_issues_kind'))
        batch_op.drop_index(batch_op.f('ix_import_issues_import_job_id'))

    op.drop_table('import_issues')
    with op.batch_alter_table('source_files', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_source_files_status'))
        batch_op.drop_index(batch_op.f('ix_source_files_sha256'))
        batch_op.drop_index(batch_op.f('ix_source_files_run_id'))
        batch_op.drop_index(batch_op.f('ix_source_files_resource_id'))
        batch_op.drop_index(batch_op.f('ix_source_files_fiscal_year'))
        batch_op.drop_index(batch_op.f('ix_source_files_dataset_slug'))

    op.drop_table('source_files')
    with op.batch_alter_table('collection_runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_collection_runs_status'))

    op.drop_table('collection_runs')
    op.drop_table('app_settings')
