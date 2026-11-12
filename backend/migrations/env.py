from logging.config import fileConfig
from alembic import context
from sqlalchemy import create_engine
from app.db import DATABASE_URL, Base
from app import models  # noqa: F401  (registers tables)

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name, disable_existing_loggers=False)
target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    """Ignore PostGIS-owned objects that are created by raw SQL in migration 0002."""
    if type_ == "table" and name == "spatial_ref_sys":
        return False
    if type_ == "column" and name == "geom":
        return False
    if type_ == "index" and name.startswith("gix_"):
        return False
    return True


def run_migrations_online():
    url = config.attributes.get("url") or DATABASE_URL
    engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=True, compare_type=True,
                          include_object=include_object)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
