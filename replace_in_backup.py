"""Put a modified ChatStorage.sqlite (and media files) into an unencrypted iPhone backup.

Usage:
    uv run replace_in_backup.py data/iphone_backup_restore/<device id> data/ChatStorage.new.sqlite [data/media_out]

Overwrites WhatsApp's ChatStorage.sqlite in the backup folder and records its new size in
the backup's Manifest.db. Files under data/media_out (from import_media.py) are added to
WhatsApp's folder in the backup at the same relative paths. Run it on a copy of a backup,
never on your only one.
"""

import hashlib
import plistlib
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
RELATIVE_PATH = "ChatStorage.sqlite"
FILE, DIRECTORY = 1, 2  # Files.flags


def file_id(relative_path: str) -> str:
    return hashlib.sha1(f"{DOMAIN}-{relative_path}".encode()).hexdigest()


def record(template: bytes, relative_path: str, size: int, inode: int) -> bytes:
    archive = plistlib.loads(template)
    objects = archive["$objects"]
    mbfile = objects[1]
    objects[mbfile["RelativePath"].data] = relative_path
    now = int(time.time())
    mbfile.update(Size=size, InodeNumber=inode, LastModified=now, LastStatusChange=now, Birth=now)
    return plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)


def add_files(backup: Path, manifest: sqlite3.Connection, media_dir: Path) -> int:
    def template(relative_path: str) -> bytes:
        return manifest.execute(
            "SELECT file FROM Files WHERE domain = ? AND relativePath = ?", (DOMAIN, relative_path)
        ).fetchone()[0]

    file_template, dir_template = template(RELATIVE_PATH), template("Message")
    existing = {p for (p,) in manifest.execute("SELECT relativePath FROM Files WHERE domain = ?", (DOMAIN,))}
    # Give every new entry an unused inode number, so none looks like a hard link.
    inode = max(plistlib.loads(b)["$objects"][1].get("InodeNumber", 0) for (b,) in manifest.execute("SELECT file FROM Files"))
    added = 0
    for path in sorted(media_dir.rglob("*")):
        relative_path = path.relative_to(media_dir).as_posix()
        if relative_path in existing:
            continue
        inode += 1
        fid = file_id(relative_path)
        if path.is_dir():
            row = (fid, DOMAIN, relative_path, DIRECTORY, record(dir_template, relative_path, 0, inode))
        else:
            target = backup / fid[:2] / fid
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(path, target)
            row = (fid, DOMAIN, relative_path, FILE, record(file_template, relative_path, path.stat().st_size, inode))
            added += 1
        manifest.execute("INSERT INTO Files (fileID, domain, relativePath, flags, file) VALUES (?, ?, ?, ?, ?)", row)
    return added


def main() -> None:
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    backup, new_file = Path(sys.argv[1]), Path(sys.argv[2])
    target = backup / file_id(RELATIVE_PATH)[:2] / file_id(RELATIVE_PATH)
    if not target.is_file():
        sys.exit(f"{target} not found: is {backup} a device backup folder?")

    manifest = sqlite3.connect(backup / "Manifest.db")
    if len(sys.argv) == 4:
        print(f"added {add_files(backup, manifest, Path(sys.argv[3]))} media files")

    shutil.copyfile(new_file, target)
    (blob,) = manifest.execute("SELECT file FROM Files WHERE fileID = ?", (file_id(RELATIVE_PATH),)).fetchone()
    archive = plistlib.loads(blob)
    archive["$objects"][1]["Size"] = target.stat().st_size
    manifest.execute(
        "UPDATE Files SET file = ? WHERE fileID = ?",
        (plistlib.dumps(archive, fmt=plistlib.FMT_BINARY), file_id(RELATIVE_PATH)),
    )
    manifest.commit()
    manifest.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    manifest.close()
    print(f"replaced {target} ({target.stat().st_size} bytes) and updated Manifest.db")


if __name__ == "__main__":
    main()
