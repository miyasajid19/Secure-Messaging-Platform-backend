"""Explicitly drop and recreate every application table.

Run from the backend directory with `python -m app.reset_database --yes`.
This permanently deletes all users, conversations, messages, stories and call
history in the PostgreSQL database configured by the backend environment.
It is intentionally never called by application startup.
"""

import argparse

from app import models  # noqa: F401 - registers every ORM table
from app.database import Base, engine


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="confirm permanent deletion of all app tables")
    args = parser.parse_args()
    if not args.yes:
        parser.error("pass --yes to confirm that all application data will be deleted")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    print("Dropped and recreated all application tables.")


if __name__ == "__main__":
    main()
