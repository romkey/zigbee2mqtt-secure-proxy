"""Alembic environment. The connection comes from db.migrate()."""

from alembic import context

from zigbee2mqtt_secure_proxy import models
from zigbee2mqtt_secure_proxy.db import Base

_ = models  # imported so the tables register on Base.metadata

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=Base.metadata)
with context.begin_transaction():
    context.run_migrations()
