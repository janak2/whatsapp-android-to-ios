"""Import WhatsApp chat history from an Android msgstore.db into an iPhone ChatStorage.sqlite.

Usage:
    uv run import_chats.py data/msgstore.db data/ChatStorage.sqlite data/ChatStorage.new.sqlite

The iPhone database is copied to the output path and only the copy is changed. Messages
already on the iPhone (same message ID in the same chat) are skipped. Media is not
imported: media messages become text placeholders like "<image>".
"""

import base64
import shutil
import sqlite3
import sys
from collections import Counter
from pathlib import Path

APPLE_EPOCH = 978307200  # CoreData stores dates as seconds since 2001-01-01
SYSTEM_MESSAGE = 7

PLACEHOLDERS = {
    1: "<image>", 2: "<audio>", 3: "<video>", 4: "<contact>", 5: "<location>",
    9: "<document>", 13: "<GIF>", 15: "<deleted message>", 16: "<live location>",
    20: "<sticker>", 42: "<view once image>", 43: "<view once video>", 90: "<call>",
}

# Values WhatsApp itself writes on nearly every row, taken from a real ChatStorage.sqlite.
MESSAGE_DEFAULTS = {
    "ZMESSAGETYPE": 0, "ZMESSAGESTATUS": 6, "ZDATAITEMVERSION": 3, "ZFLAGS": 16777216,
    "ZDOCID": 0, "ZSPOTLIGHTSTATUS": -32768, "ZGROUPEVENTTYPE": 2, "ZMESSAGEERRORSTATUS": 0,
    "ZENCRETRYCOUNT": 0, "ZFILTEREDRECIPIENTCOUNT": 0, "ZCHILDMESSAGESDELIVEREDCOUNT": 0,
    "ZCHILDMESSAGESPLAYEDCOUNT": 0, "ZCHILDMESSAGESREADCOUNT": 0,
}
CHAT_DEFAULTS = {
    "ZCONTACTABID": 0, "ZFLAGS": 272, "ZHIDDEN": 0, "ZIDENTITYVERIFICATIONEPOCH": -1,
    "ZIDENTITYVERIFICATIONSTATE": 0, "ZREMOVED": 0, "ZUNREADCOUNT": 0,
}
MEMBER_DEFAULTS = {"ZCONTACTNAME": "", "ZISACTIVE": 0, "ZISADMIN": 0, "ZCONTACTABID": 0}

# ZPUSHNAME holds a base64 protobuf: incoming messages carry field 4 = 0, outgoing ones also
# carry field 3 = our own JID.
INCOMING_METADATA = base64.b64encode(b"\x20\x00").decode()

ANDROID_MESSAGES = """
SELECT m.key_id, m.from_me, m.timestamp, m.message_type, m.text_data,
       cj.raw_string, cj.server, c.subject, c.created_timestamp, c.archived, sj.raw_string
FROM message m
JOIN chat c ON c._id = m.chat_row_id
JOIN jid cj ON cj._id = c.jid_row_id
LEFT JOIN jid sj ON sj._id = m.sender_jid_row_id
WHERE c.hidden = 0 AND cj.server IN ('s.whatsapp.net', 'lid', 'g.us')
  AND m.message_type != ? AND m.key_id IS NOT NULL AND m.timestamp > 0
ORDER BY m.timestamp, m._id
"""


def outgoing_metadata(own_jid: str) -> str:
    raw = own_jid.encode()
    return base64.b64encode(b"\x1a" + bytes([len(raw)]) + raw + b"\x20\x00").decode()


def message_text(message_type: int, text: str | None) -> str | None:
    if message_type == 0:
        return text
    label = PLACEHOLDERS.get(message_type)
    if label is None:
        return text
    return f"{label}: {text}" if text else label


def mask(jid: str | None) -> str:
    if not jid:
        return "unknown"
    user, _, server = jid.partition("@")
    return "*" * (len(user) - 4) + user[-4:] + "@" + server


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    android_path, ios_path, out_path = sys.argv[1:]
    for leftover in (out_path + "-wal", out_path + "-shm"):
        Path(leftover).unlink(missing_ok=True)
    shutil.copyfile(ios_path, out_path)

    android = sqlite3.connect(Path(android_path).resolve().as_uri() + "?mode=ro", uri=True)
    ios = sqlite3.connect(out_path)

    pn_to_lid: dict[str, str] = {}
    lid_to_pn: dict[str, str] = {}
    for lid, pn in android.execute(
        "SELECT l.raw_string, p.raw_string FROM jid_map m"
        " JOIN jid l ON l._id = m.lid_row_id JOIN jid p ON p._id = m.jid_row_id"
    ):
        pn_to_lid[pn] = lid
        lid_to_pn[lid] = pn

    # We are a member of every group we're in, so our JIDs are the most widespread members.
    def most_common_member(suffix: str) -> str | None:
        row = ios.execute(
            "SELECT ZMEMBERJID FROM ZWAGROUPMEMBER WHERE ZMEMBERJID LIKE ?"
            " GROUP BY 1 ORDER BY COUNT(DISTINCT ZCHATSESSION) DESC LIMIT 1", ("%" + suffix,)
        ).fetchone()
        return row[0] if row else None

    own_lid = most_common_member("@lid")
    own_pn = lid_to_pn.get(own_lid or "") or most_common_member("@s.whatsapp.net")
    print(f"own phone JID: {mask(own_pn)}, own LID: {mask(own_lid)}")
    group_metadata = outgoing_metadata(own_lid or own_pn or "")
    direct_metadata = outgoing_metadata(own_pn or own_lid or "")

    entity = dict(ios.execute("SELECT Z_NAME, Z_ENT FROM Z_PRIMARYKEY"))
    next_pk = {name: z_max + 1 for name, z_max in ios.execute("SELECT Z_NAME, Z_MAX FROM Z_PRIMARYKEY")}

    def insert(table: str, name: str, values: dict) -> int:
        pk = next_pk[name]
        next_pk[name] += 1
        row = {"Z_PK": pk, "Z_ENT": entity[name], "Z_OPT": 1, **values}
        ios.execute(
            f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
            list(row.values()),
        )
        return pk

    chats = {jid: pk for pk, jid in ios.execute("SELECT Z_PK, ZCONTACTJID FROM ZWACHATSESSION")}
    members = {
        (chat, jid): pk
        for pk, chat, jid in ios.execute("SELECT Z_PK, ZCHATSESSION, ZMEMBERJID FROM ZWAGROUPMEMBER")
    }
    seen = set(ios.execute("SELECT ZCHATSESSION, ZSTANZAID FROM ZWAMESSAGE"))
    touched: set[int] = set()
    counts: Counter[str] = Counter()

    for (key_id, from_me, timestamp, message_type, text_data, chat_jid, server, subject,
         created, archived, sender) in android.execute(ANDROID_MESSAGES, (SYSTEM_MESSAGE,)):
        text = message_text(message_type, text_data)
        if not text:
            counts["skipped: no text and no known type"] += 1
            continue
        date = timestamp / 1000 - APPLE_EPOCH
        is_group = server == "g.us"
        if is_group:
            jid, phone = chat_jid, None
        elif server == "lid":
            jid, phone = chat_jid, lid_to_pn.get(chat_jid)
        else:
            jid, phone = pn_to_lid.get(chat_jid) or chat_jid, chat_jid

        chat = chats.get(jid)
        if chat is None:
            name = (subject or "") if is_group else "+" + (phone or jid).split("@")[0]
            chat = chats[jid] = insert("ZWACHATSESSION", "WAChatSession", {
                "ZCONTACTJID": jid, "ZPARTNERNAME": name,
                "ZCONTACTIDENTIFIER": phone if jid.endswith("@lid") else None,
                "ZARCHIVED": 1 if archived == 1 else 0, "ZSESSIONTYPE": 1 if is_group else 0,
                "ZSPOTLIGHTSTATUS": 1 if is_group else -5, "ZETAG": "lid" if is_group else None,
                "ZMESSAGECOUNTER": 1, **CHAT_DEFAULTS,
            })
            if is_group:
                created_date = created / 1000 - APPLE_EPOCH if created else date
                info = insert("ZWAGROUPINFO", "WAGroupInfo", {
                    "ZCHATSESSION": chat, "ZCREATIONDATE": created_date,
                    "ZSUBJECTTIMESTAMP": created_date, "ZSTATE": 0,
                })
                ios.execute("UPDATE ZWACHATSESSION SET ZGROUPINFO = ? WHERE Z_PK = ?", (info, chat))
            counts["chats created"] += 1

        if (chat, key_id) in seen:
            counts["skipped: already on the iPhone"] += 1
            continue
        seen.add((chat, key_id))

        member = None
        if is_group and not from_me and sender:
            sender = pn_to_lid.get(sender, sender)
            member = members.get((chat, sender))
            if member is None:
                member = members[(chat, sender)] = insert(
                    "ZWAGROUPMEMBER", "WAGroupMember",
                    {"ZCHATSESSION": chat, "ZMEMBERJID": sender, **MEMBER_DEFAULTS},
                )
                counts["group members created"] += 1

        insert("ZWAMESSAGE", "WAMessage", {
            "ZCHATSESSION": chat, "ZGROUPMEMBER": member, "ZISFROMME": from_me,
            "ZFROMJID": None if from_me else jid, "ZTOJID": jid if from_me else None,
            "ZTEXT": text, "ZMESSAGEDATE": date, "ZSENTDATE": date if from_me else None,
            "ZSORT": 0, "ZSTANZAID": key_id,
            "ZPUSHNAME": (group_metadata if is_group else direct_metadata) if from_me else INCOMING_METADATA,
            **MESSAGE_DEFAULTS,
        })
        touched.add(chat)
        counts["messages imported"] += 1

    # Order each changed chat by date and point it at its newest message.
    for chat in touched:
        rows = ios.execute(
            "SELECT Z_PK, ZTEXT, ZMESSAGEDATE FROM ZWAMESSAGE WHERE ZCHATSESSION = ?"
            " ORDER BY ZMESSAGEDATE, Z_PK", (chat,)
        ).fetchall()
        ios.executemany(
            "UPDATE ZWAMESSAGE SET ZSORT = ? WHERE Z_PK = ?",
            [(n, pk) for n, (pk, _, _) in enumerate(rows, 1)],
        )
        last_pk, last_text, last_date = rows[-1]
        old_last, counter = ios.execute(
            "SELECT ZLASTMESSAGE, ZMESSAGECOUNTER FROM ZWACHATSESSION WHERE Z_PK = ?", (chat,)
        ).fetchone()
        if old_last != last_pk:
            ios.execute("UPDATE ZWAMESSAGE SET ZLASTSESSION = NULL WHERE ZLASTSESSION = ?", (chat,))
            ios.execute("UPDATE ZWAMESSAGE SET ZLASTSESSION = ? WHERE Z_PK = ?", (chat, last_pk))
            ios.execute(
                "UPDATE ZWACHATSESSION SET ZLASTMESSAGE = ?, ZLASTMESSAGEDATE = ?,"
                " ZLASTMESSAGETEXT = ? WHERE Z_PK = ?", (last_pk, last_date, last_text, chat),
            )
        ios.execute(
            "UPDATE ZWACHATSESSION SET ZMESSAGECOUNTER = ?, Z_OPT = Z_OPT + 1 WHERE Z_PK = ?",
            (max(counter or 0, len(rows) + 1), chat),
        )

    ios.executemany(
        "UPDATE Z_PRIMARYKEY SET Z_MAX = ? WHERE Z_NAME = ?",
        [(pk - 1, name) for name, pk in next_pk.items()],
    )
    ios.commit()
    ios.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    ios.close()

    for key, value in sorted(counts.items()):
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
