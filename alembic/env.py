from logging.config import fileConfig
import asyncio
from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config
from xuanshu_platform.config import settings
from xuanshu_platform.db import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata

def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata,
                      compare_type=True, compare_server_default=True)
    with context.begin_transaction():
        context.run_migrations()

async def run_async_migrations():
    configuration = config.get_section(config.config_ini_section) or {}
    configuration['sqlalchemy.url'] = settings.database_url
    connectable = async_engine_from_config(configuration, prefix='sqlalchemy.', poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()

def run_migrations_online():
    asyncio.run(run_async_migrations())

if context.is_offline_mode():
    raise RuntimeError('XuanShu migrations require an online PostgreSQL connection')
run_migrations_online()
