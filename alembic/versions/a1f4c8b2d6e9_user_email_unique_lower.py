"""train.Q3 #74 — normalize User.email + UNIQUE index on lower(email)

Revision ID: a1f4c8b2d6e9
Revises: c7d2e9f4a1b8
Create Date: 2026-08-24 12:00:00.000000

Round-3 hardening finding #74 (LOW, identity ambiguity). `User.email` carried only the
NON-unique ix_user_email. The app layer blocks duplicates (security.create_user raises on
get_by_email), but that is a read-check-then-INSERT with nothing behind it: two concurrent
creates, a direct DB write, or an SSO/SCIM-provisioned path can land two rows for one
address — and every by-email lookup in the tree is `select(User).where(...).first()`, so
the account you get back is then arbitrary. Identity must not be a coin flip. Same class,
and the same structural answer, as c2f1a7d4e9b6 (segmenttm upsert uniqueness).

  ux_user_email_lower — UNIQUE over lower(email). A FUNCTIONAL index (the
                        ux_usergroup_org_name / ux_project_inbox_key precedent) rather
                        than a plain UNIQUE(email), because the app's identity law is the
                        NORMALIZED address: security.get_by_email lowercases its argument
                        before comparing, so 'Bob@x.test' and 'bob@x.test' are already ONE
                        identity to every reader. A plain UNIQUE(email) would happily
                        accept both and hand the reader whichever the planner returned
                        first. Portable: SQLite and Postgres both index expressions.

Three steps, in order, all idempotent:

  1. NORMALIZE every stored address to lower(trim(...)). This is a fix in its own right —
     a mixed-case row was already UNREACHABLE through get_by_email (which lowercases the
     needle but not the haystack), so such an account could not log in or be looked up.

  2. DEDUPE what step 1 may have collided. KEEP-RULE: an ACTIVE account that has signed in
     (most recent last_login) outranks an inactive or never-used one (a mixed-case row stored
     before step 1 could never log in, so the first-registered twin is often the dead one);
     among equals LOWEST id WINS (the ORIGINAL
     account — the opposite of segmenttm's newest-wins, deliberately: for a TM row the
     last write is the truth, for an identity the first-registered account is the real
     one and the later collision is the accident). The losers are TOMBSTONED, never
     DELETEd: a User row is an FK target (apikey.user_id, projectmember.user_id,
     usergroupmember.user_id) and an audit-trail referent, so deleting one would orphan
     live rows to fix a naming problem. A loser gets email 'duplicate-user-<id>@dedup.
     invalid' (the gdpr._tombstone 'deleted-user-<id>@erased.invalid' convention: unique
     by construction since <id> is the PK, lowercase, RFC-2606 unroutable) and active=false,
     and its API keys are revoked (#422: a later hand reinstatement revives no credential),
     so it cannot log in and cannot be resolved by email, while every FK still points at
     a real row. Each loser is LOGGED at WARNING with its id, its original address, and
     the id of the winner it collided with — an operator must be able to see exactly which
     accounts were retired and reinstate one by hand if the wrong twin lost.

     Accounts with NO address at all are not twins of one another: they are never retired. The
     first keeps the empty value; the rest get a unique 'no-email-user-<id>@dedup.invalid'
     placeholder (so the index can be built) and stay active. A placeholder is NOT an
     identity: security.is_placeholder_email makes every by-email door (password sign-in,
     invites, SSO/SAML/LDAP/SCIM linking, account creation, outgoing mail) refuse it, so an
     emailless account that has a password cannot sign in by typing its placeholder.

  3. CREATE the UNIQUE index.

The SAME index rides app/db._UNIQUE_INDEXES for the create_all / legacy-shim SQLite path,
which SKIPS-with-warning if duplicates are present (the segmenttm house style — a shim DB
cannot run the dedupe above, and init_db must not crash on it), so a non-alembic boot
builds the identically-named index whenever it legally can.

downgrade drops only the index. The normalization + tombstones are DATA and are not
reversed (standard for a data migration, and the same choice c2f1a7d4e9b6 made).
"""

import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1f4c8b2d6e9"
down_revision: Union[str, None] = "c7d2e9f4a1b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

log = logging.getLogger("alembic.runtime.migration")

# 'user' is a reserved word in Postgres, so every raw statement quotes it.
_T = '"user"'


def upgrade() -> None:
    conn = op.get_bind()

    # The addresses AS STORED, captured before step 1 rewrites them: a loser's log line
    # must name what the operator will recognise ('Alice@X.test'), not the normalized
    # form every twin collapses to.
    as_stored = {
        uid: email
        for uid, email in conn.execute(sa.text("SELECT id, email FROM %s" % _T)).fetchall()
    }

    # 1) NORMALIZE: an address that is not already lower/trimmed was unreachable through
    #    security.get_by_email (which lowercases the needle, never the haystack).
    normalized = conn.execute(
        sa.text(
            "UPDATE %s SET email = lower(trim(email)) "
            "WHERE email IS NOT NULL AND email <> lower(trim(email))" % _T
        )
    ).rowcount
    if normalized:
        log.warning(
            "#74: normalized %s user email(s) to lowercase (a mixed-case address was "
            "unreachable through get_by_email).",
            normalized,
        )

    # 2) DEDUPE (keep-rule below). Read the whole (id, email) list rather than doing it in
    #    SQL: the LOSERS must be named individually in the log, and the table is small
    #    (accounts, not rows-per-document).
    rows = conn.execute(sa.text("SELECT id, email FROM %s ORDER BY id" % _T)).fetchall()

    # WHICH twin keeps the address. Lowest id is the TIEBREAK, but an ACTIVE account that has
    # actually signed in outranks an inactive or never-used one: a mixed-case row stored
    # before normalization could never be reached by get_by_email, so the first-registered
    # twin is often the dead one while the lowercase twin is the account the person uses
    # (with its memberships and keys). Evidence of use is read from the same table
    # (active, last_login); with no evidence on either side the old rule (lowest id) stands.
    best = {}  # normalized address -> (use rank, id) of the twin that keeps it
    for rid, remail, ractive, rlogin in conn.execute(
        sa.text("SELECT id, email, active, last_login FROM %s ORDER BY id" % _T)
    ).fetchall():
        rkey = (remail or "").lower().strip()
        if not rkey:
            continue  # an EMPTY address is not a twin of another empty one; see below
        rank = (1 if ractive else 0, str(rlogin) if rlogin else "")
        # Strictly greater only, and ids arrive ascending: a tie keeps the lowest id.
        if rkey not in best or rank > best[rkey][0]:
            best[rkey] = (rank, rid)
    keeper = {k: v[1] for k, v in best.items()}
    empty_kept = False
    for uid, email in rows:
        key = (email or "").lower().strip()
        if not key:
            # Several accounts can legitimately carry NO address (an SSO/SCIM row the IdP gave
            # none for). They are not duplicates of one another, so they are NOT retired: the
            # first keeps the empty value, the rest get a unique unroutable placeholder so the
            # UNIQUE index can be built, and every one of them stays active.
            if empty_kept:
                placeholder = "no-email-user-%d@dedup.invalid" % uid
                log.warning(
                    "#74: user #%s has no email address; given the placeholder %r so the "
                    "unique index can be built. The account stays ACTIVE.",
                    uid,
                    placeholder,
                )
                conn.execute(
                    sa.text("UPDATE %s SET email = :t WHERE id = :i" % _T),
                    {"t": placeholder, "i": uid},
                )
            empty_kept = True
            continue
        if keeper[key] != uid:
            tomb = "duplicate-user-%d@dedup.invalid" % uid
            log.warning(
                "#74 DEDUPE: user #%s (stored as %r, normalizes to %r) is a DUPLICATE of "
                "user #%s (stored as %r), which KEEPS the address (active, latest sign-in, "
                "then lowest id). Retiring #%s as %r + active=false and revoking its API "
                "keys -- it is NOT deleted (FK references survive); reinstate it by hand "
                "and issue new keys if the wrong twin lost.",
                uid,
                as_stored.get(uid, email),
                key,
                keeper[key],
                as_stored.get(keeper[key], key),
                uid,
                tomb,
            )
            # `active = false`, not `active = 0`: Postgres has no implicit int-to-boolean
            # cast, so an integer here aborts the migration on exactly the Postgres installs
            # that have duplicate emails (SQLite accepts both, keeping tests green). The
            # `false` literal is valid on both engines (SQLite 3.23+).
            conn.execute(
                sa.text("UPDATE %s SET email = :t, active = false WHERE id = :i" % _T),
                {"t": tomb, "i": uid},
            )
            # #422 posture: retiring an account ends its credentials for good, so reinstating
            # the row by hand later never revives a key nobody re-issued. (Its sessions die
            # with `active`: every session check reads the live row.) Idempotent: a re-run
            # finds no active key left on a tombstone.
            conn.execute(
                sa.text("UPDATE apikey SET active = false WHERE user_id = :i AND active = true"),
                {"i": uid},
            )

    # 3) The UNIQUE index over the NORMALIZED address.
    op.create_index("ux_user_email_lower", "user", [sa.text("lower(email)")], unique=True)


def downgrade() -> None:
    # IF EXISTS, because this index may already be gone (#158). It is EXPRESSION-BASED
    # (lower(email)), and SQLAlchemy cannot reflect an expression index -- so any
    # `batch_alter_table('user')` anywhere LATER in the chain, which on SQLite is a full
    # table rebuild, silently drops it and does not put it back:
    #   SAWarning: Skipped unsupported reflection of expression-based index
    #              ux_user_email_lower
    # `b4e1c7f29a06` (User.session_epoch) is that op today: downgrading past it rebuilds
    # `user`, the index goes with the old table, and this unconditional drop then failed
    # the whole chain with "no such index". Both SQLite and Postgres support IF EXISTS on
    # DROP INDEX, so one statement covers both dialects; op.drop_index has no portable
    # if_exists on this alembic version.
    op.execute("DROP INDEX IF EXISTS ux_user_email_lower")
