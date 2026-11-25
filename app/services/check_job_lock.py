"""PostgreSQL ownership for a claimed check and its processing subprocess."""
from contextlib import contextmanager

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session


# A two-integer advisory lock key keeps job IDs separate from other locks.
CHECK_JOB_LOCK_NAMESPACE = 7419842


class CheckJobBusy(RuntimeError):
    """A recovery transaction is changing the check's state."""


@contextmanager
def hold_check_job(bind, check_id: int):
    """Keep a shared lock until this owner's physical connection closes."""
    if bind.dialect.name != "postgresql":  # Isolated SQLite tests.
        yield None
        return

    with bind.connect() as connection:
        # Session-level locks must never be returned to the connection pool.
        connection.detach()
        acquired = connection.scalar(select(func.pg_try_advisory_lock_shared(
            CHECK_JOB_LOCK_NAMESPACE, check_id,
        )))
        connection.commit()
        if not acquired:
            raise CheckJobBusy()

        locked_connection = connection.connection.dbapi_connection

        def require_lock_connection(current, *_):
            # A reconnect has a new PostgreSQL session and lost the lock.
            if current.connection.dbapi_connection is not locked_connection:
                raise RuntimeError("Check job lost its PostgreSQL connection.")

        event.listen(connection, "before_cursor_execute", require_lock_connection)
        try:
            yield connection
        finally:
            event.remove(connection, "before_cursor_execute", require_lock_connection)


def try_recover_check(db: Session, check_id: int) -> bool:
    """Reserve recovery until commit, provided no worker still owns the job."""
    if db.get_bind().dialect.name != "postgresql":
        return True
    return bool(db.scalar(select(func.pg_try_advisory_xact_lock(
        CHECK_JOB_LOCK_NAMESPACE, check_id,
    ))))
