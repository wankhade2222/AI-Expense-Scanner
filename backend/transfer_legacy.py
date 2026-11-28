"""Transfer pre-sign-in documents to one existing workspace."""

import argparse

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.auth import Account
from backend.main import Receipt, engine


def transfer_legacy(session: Session, email: str) -> int:
    workspace_id = session.scalar(select(Account.workspace_id).where(Account.email == email.strip().casefold()))
    if workspace_id is None:
        raise ValueError("Create the destination account first.")
    return session.execute(
        update(Receipt).where(Receipt.workspace_id == "local-user").values(workspace_id=workspace_id),
    ).rowcount


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-email", required=True, help="Account that should receive the legacy documents.")
    args = parser.parse_args()
    with Session(engine) as session:
        try:
            count = transfer_legacy(session, args.account_email)
            session.commit()
        except ValueError as exc:
            parser.error(str(exc))
        except IntegrityError:
            parser.error("A legacy document duplicates an upload in the destination workspace. No documents were moved.")
    print(f"Transferred {count} legacy document(s).")


if __name__ == "__main__":
    main()
