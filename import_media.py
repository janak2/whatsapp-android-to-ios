"""Turn the "<image>"-style placeholders from import_chats.py into real media messages.

Usage:
    uv run import_media.py data/msgstore.db data/android_media data/ChatStorage.sqlite \\
        data/ChatStorage.new.sqlite data/media_out

data/android_media is WhatsApp's media folder pulled from the Android phone:
    adb pull /sdcard/Android/media/com.whatsapp/WhatsApp/Media data/android_media/

Every photo, video, GIF, audio file, document and sticker whose file is still there is
copied to data/media_out/Message/Media/ (with a thumbnail where WhatsApp makes one), and
its placeholder becomes a media message. The iPhone database is copied to the output path
and only the copy is changed. replace_in_backup.py puts data/media_out into the backup.
This is for the account whose ChatStorage.sqlite sits at the top of WhatsApp's folder.
"""

import base64
import hashlib
import mimetypes
import shutil
import sqlite3
import subprocess
import sys
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from import_chats import APPLE_EPOCH, PLACEHOLDERS

IMAGE, VIDEO, AUDIO, DOCUMENT, STICKER = 1, 2, 3, 8, 15
IOS_TYPES = {1: IMAGE, 3: VIDEO, 13: VIDEO, 2: AUDIO, 9: DOCUMENT, 20: STICKER}
KINDS = {IMAGE: "photos", VIDEO: "videos and GIFs", AUDIO: "audio", DOCUMENT: "documents", STICKER: "stickers"}
MEDIA_FLAGS = 83886080  # ZFLAGS WhatsApp writes on media messages
THUMB_SIZE = 72  # WhatsApp's .thumb files are JPEGs at most 72 pixels on a side

ANDROID_MEDIA = """
SELECT m.key_id, m.timestamp, m.message_type, m.text_data, mm.file_path, mm.mime_type,
       mm.width, mm.height, mm.media_duration, mm.page_count, mm.media_name,
       mm.message_url, mm.media_key, mm.enc_file_hash, mm.media_key_timestamp
FROM message m JOIN message_media mm ON mm.message_row_id = m._id
WHERE m.message_type IN (1, 2, 3, 9, 13, 20) AND mm.file_path IS NOT NULL AND mm.file_path != ''
"""


def varint(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append(n & 0x7F | 0x80)
        n >>= 7
    return bytes(out + bytes([n]))


def media_key(key: bytes, enc_file_hash: str | None, timestamp: int | None) -> bytes:
    # protobuf: 1 = media key, 2 = SHA-256 of the encrypted file, 3 = key time (s), 4 = 0
    out = b"\x0a" + varint(len(key)) + key
    if enc_file_hash:
        enc = base64.b64decode(enc_file_hash)
        out += b"\x12" + varint(len(enc)) + enc
    seconds = timestamp or 0
    while seconds > 10**11:  # Android stores it in micro- or milliseconds
        seconds //= 1000
    return out + b"\x18" + varint(seconds) + b"\x20\x00"


def make_thumb(src: Path, ios_type: int, dest: Path) -> bool:
    jpg = dest.with_suffix(".jpg")
    try:
        if ios_type == IMAGE:
            with Image.open(src) as image:
                thumb = ImageOps.exif_transpose(image).convert("RGB")
                thumb.thumbnail((THUMB_SIZE, THUMB_SIZE))
                thumb.save(dest, "JPEG", quality=70)
            return True
        if ios_type == VIDEO:
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-frames:v", "1",
                 "-vf", f"scale={THUMB_SIZE}:{THUMB_SIZE}:force_original_aspect_ratio=decrease",
                 "-f", "image2", "-c:v", "mjpeg", str(dest)],
                check=True, capture_output=True, timeout=60,
            )
            return dest.exists()
        if ios_type == DOCUMENT and src.suffix.lower() == ".pdf":
            subprocess.run(
                ["pdftoppm", "-jpeg", "-f", "1", "-l", "1", "-scale-to", str(THUMB_SIZE),
                 "-singlefile", str(src), str(jpg.with_suffix(""))],
                check=True, capture_output=True, timeout=60,
            )
            jpg.rename(dest)
            return True
    except Exception:
        dest.unlink(missing_ok=True)
        jpg.unlink(missing_ok=True)
    return False


def main() -> None:
    if len(sys.argv) != 6:
        sys.exit(__doc__)
    android_path, media_dir, ios_path, out_path, out_dir = sys.argv[1:]
    for leftover in (out_path + "-wal", out_path + "-shm"):
        Path(leftover).unlink(missing_ok=True)
    shutil.copyfile(ios_path, out_path)

    android = sqlite3.connect(Path(android_path).resolve().as_uri() + "?mode=ro", uri=True)
    ios = sqlite3.connect(out_path)

    placeholders: dict[str, list[tuple[int, float, str, str]]] = {}
    for pk, stanza, date, text, jid in ios.execute(
        "SELECT m.Z_PK, m.ZSTANZAID, m.ZMESSAGEDATE, m.ZTEXT, c.ZCONTACTJID FROM ZWAMESSAGE m"
        " JOIN ZWACHATSESSION c ON c.Z_PK = m.ZCHATSESSION"
        " WHERE m.ZMESSAGETYPE = 0 AND m.ZMEDIAITEM IS NULL AND m.ZTEXT LIKE '<%'"
    ):
        placeholders.setdefault(stanza, []).append((pk, date, text, jid))

    entity, z_max = ios.execute("SELECT Z_ENT, Z_MAX FROM Z_PRIMARYKEY WHERE Z_NAME = 'WAMediaItem'").fetchone()
    next_pk = z_max + 1
    counts: Counter[str] = Counter()

    for (key_id, timestamp, android_type, text, file_path, mime, width, height, duration, pages,
         name, url, key, enc_file_hash, key_timestamp) in android.execute(ANDROID_MEDIA):
        date = timestamp / 1000 - APPLE_EPOCH
        label = PLACEHOLDERS[android_type]
        matches = [
            (pk, jid) for pk, ios_date, ios_text, jid in placeholders.get(key_id, [])
            if abs(ios_date - date) < 1 and (ios_text == label or ios_text.startswith(label + ": "))
        ]
        src = Path(media_dir) / file_path
        if not matches:
            counts["skipped: message not on the iPhone"] += 1
            continue
        if not src.is_file():
            counts["skipped: file no longer on the Android phone"] += 1
            continue

        ios_type = IOS_TYPES[android_type]
        file_hash = base64.b64encode(hashlib.sha256(src.read_bytes()).digest()).decode()
        if ios_type == DOCUMENT:
            title = name or text or src.name
            caption = text if text and text != title else None
        else:
            title, caption = None, text
        section = datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m")

        for pk, jid in matches:
            u = str(uuid.uuid4())
            local_path = f"Media/{jid}/{u[0]}/{u[1]}/{u}{src.suffix.lower()}"
            dest = Path(out_dir) / "Message" / local_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            thumb = dest.with_suffix(".thumb")
            has_thumb = make_thumb(src, ios_type, thumb)

            row = {
                "Z_PK": next_pk, "Z_ENT": entity, "Z_OPT": 1, "ZMESSAGE": pk,
                "ZFILESIZE": dest.stat().st_size, "ZMEDIAORIGIN": 1 if ios_type == AUDIO else 0,
                "ZMOVIEDURATION": (pages if ios_type == DOCUMENT else duration if ios_type in (VIDEO, AUDIO) else 0) or 0,
                "ZASPECTRATIO": 0.0, "ZHACCURACY": 0.0,
                "ZLATITUDE": float(width or 0) if ios_type in (IMAGE, VIDEO) else 0.0,
                "ZLONGITUDE": float(height or 0) if ios_type in (IMAGE, VIDEO) else 0.0,
                "ZMEDIAURLDATE": date, "ZMEDIALOCALPATH": local_path,
                "ZXMPPTHUMBPATH": local_path.rsplit(".", 1)[0] + ".thumb" if has_thumb else None,
                "ZMEDIAURL": url, "ZMEDIAKEY": media_key(key, enc_file_hash, key_timestamp) if key else None,
                "ZVCARDNAME": file_hash,
                "ZVCARDSTRING": mime or mimetypes.guess_type(src.name)[0] or "application/octet-stream",
                "ZAUTHORNAME": title, "ZTITLE": caption,
            }
            ios.execute(
                f"INSERT INTO ZWAMEDIAITEM ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                list(row.values()),
            )
            ios.execute(
                "UPDATE ZWAMESSAGE SET ZMESSAGETYPE = ?, ZMEDIAITEM = ?, ZFLAGS = ?, ZTEXT = ?,"
                " ZMEDIASECTIONID = ?, Z_OPT = Z_OPT + 1 WHERE Z_PK = ?",
                (ios_type, next_pk, MEDIA_FLAGS, title,
                 section if ios_type in (IMAGE, VIDEO, DOCUMENT) else None, pk),
            )
            next_pk += 1
            counts[f"imported: {KINDS[ios_type]}"] += 1
            counts["thumbnails made"] += has_thumb

    ios.execute("UPDATE Z_PRIMARYKEY SET Z_MAX = ? WHERE Z_NAME = 'WAMediaItem'", (next_pk - 1,))
    ios.commit()
    ios.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    ios.close()

    for label, value in sorted(counts.items()):
        print(f"{label}: {value}")


if __name__ == "__main__":
    main()
