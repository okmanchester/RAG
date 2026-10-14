"""Online application connection, or credential-free MySQL offline SQL."""
from alembic import context

from rag.management.config import Settings
from rag.management.models import Base


def run(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(dialect_name="mysql", target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
elif context.config.attributes.get("connection") is not None:
    run(context.config.attributes["connection"])
else:
    engine = Settings.from_env().engine()
    try:
        with engine.connect() as connection:
            run(connection)
    finally:
        engine.dispose()
