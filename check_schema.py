"""Check that a WhatsApp database still has the tables and columns watoi relies on.

Usage:
    uv run check_schema.py android data/msgstore.db
    uv run check_schema.py ios data/ChatStorage.sqlite
"""

import sqlite3
import sys
from pathlib import Path

# Tables and columns read by watoi's importer (watoi/main.m).
ANDROID = {
    "chat_view": ["raw_string_jid", "hidden", "archived", "subject", "created_timestamp"],
    "group_participants": ["gjid", "jid", "admin"],
    "legacy_available_messages_view": [
        "key_remote_jid", "key_from_me", "key_id", "remote_resource", "status",
        "timestamp", "media_wa_type", "data", "media_caption",
    ],
}

# Tables and columns written by watoi's importer. CoreData stores field `contactJID`
# of entity `WAChatSession` as column ZCONTACTJID of table ZWACHATSESSION.
IOS = {
    "Z_PRIMARYKEY": ["Z_ENT", "Z_NAME", "Z_MAX"],
    "ZWACHATSESSION": [
        "ZCONTACTJID", "ZPARTNERNAME", "ZARCHIVED", "ZMESSAGECOUNTER", "ZGROUPINFO",
        "ZLASTMESSAGE", "ZLASTMESSAGETEXT", "ZLASTMESSAGEDATE",
    ],
    "ZWAGROUPINFO": ["ZCHATSESSION", "ZCREATIONDATE"],
    "ZWAGROUPMEMBER": ["ZCHATSESSION", "ZMEMBERJID", "ZCONTACTNAME", "ZISADMIN", "ZISACTIVE"],
    "ZWAMESSAGE": [
        "ZCHATSESSION", "ZGROUPMEMBER", "ZISFROMME", "ZFROMJID", "ZTOJID", "ZTEXT",
        "ZMESSAGEDATE", "ZMESSAGESTATUS", "ZMESSAGETYPE", "ZSORT", "ZSTANZAID",
        "ZDATAITEMVERSION",
    ],
}


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def always_filled(conn: sqlite3.Connection, table: str, known: list[str]) -> list[str]:
    """Columns that are non-empty in every existing row but that watoi never sets."""
    others = [c for c in columns(conn, table) if c not in known and not c.startswith("Z_")]
    if not others:
        return []
    counts = conn.execute(
        "SELECT COUNT(*), " + ", ".join(f'COUNT("{c}")' for c in others) + f' FROM "{table}"'
    ).fetchone()
    total = counts[0]
    return [c for c, n in zip(others, counts[1:]) if total and n == total]


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in ("android", "ios"):
        sys.exit(__doc__)
    platform, path = sys.argv[1], sys.argv[2]
    expected = ANDROID if platform == "android" else IOS
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)

    ok = True
    for table, wanted in expected.items():
        found = columns(conn, table)
        if not found:
            print(f"MISSING table {table}")
            ok = False
            continue
        missing = [c for c in wanted if c not in found]
        if missing:
            print(f"MISSING {table}: {', '.join(missing)}")
            ok = False
        else:
            print(f"OK      {table}")
        if platform == "ios":
            filled = always_filled(conn, table, wanted)
            if filled:
                print(f"        filled in every row but not set by watoi: {', '.join(filled)}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
