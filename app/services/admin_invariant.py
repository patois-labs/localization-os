"""Database serialization for account changes that can remove administrators.

Documented in: docs/architecture/identity-and-access.md
"""

from sqlalchemy import text


def lock_user_mutations(session) -> None:
    """Hold the administrator guard until the caller commits or rolls back.

    Use one install-wide lock, including when multi-org is enabled: this also
    serializes moves between organizations and the legacy NULL organization.
    Reads of the target, caller scope and administrator count MUST follow this
    call, and writes must use this same Session. Never open a second writer
    inside the guarded transaction (in particular, API-key revocation).

    SQLite's no-op UPDATE acquires its database writer reservation even when
    there are no users. PostgreSQL's advisory lock works across processes;
    READ COMMITTED ensures a waiter sees the preceding transaction's commit.
    """
    with session.no_autoflush:
        connection = session.connection()
        if connection.dialect.name == "sqlite":
            session.execute(text('UPDATE "user" SET id = id WHERE 1 = 0'))
        elif connection.dialect.name == "postgresql":
            if connection.get_isolation_level() != "READ COMMITTED":
                raise RuntimeError("Administrator mutations require READ COMMITTED isolation")
            session.execute(text("SELECT pg_advisory_xact_lock(400, 178)"))
        else:
            raise RuntimeError("Unsupported database for administrator mutations")
