"""Single source of truth for SQLite schema versions.

Every store owns its migrations, but the *latest* version numbers live here so
consumers that must agree on them (stores, stack backup validation, tests)
cannot drift apart independently. Bump the constant together with the new
migration in the owning store.
"""

MEMORY_SCHEMA_VERSION = 6
KNOWLEDGE_SCHEMA_VERSION = 2
AUTH_SCHEMA_VERSION = 2
