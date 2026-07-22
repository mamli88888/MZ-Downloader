from __future__ import annotations

import json

from telethon.sessions import SQLiteSession, StringSession

from config import SETTINGS


def main() -> None:
    exported = []
    for account in SETTINGS.accounts:
        if account.string_session:
            session_string = account.string_session
        else:
            session = SQLiteSession(str(account.session_path))
            try:
                session_string = StringSession.save(session)
            finally:
                session.close()
        if not session_string:
            raise RuntimeError(f"Session {account.name} is not authorized")
        exported.append(
            {
                "name": account.name,
                "api_id": account.api_id,
                "api_hash": account.api_hash,
                "string_session": session_string,
            }
        )
    print("TELEGRAM_ACCOUNTS=" + json.dumps(exported, separators=(",", ":")))


if __name__ == "__main__":
    main()
