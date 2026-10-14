"""Put a modified ChatStorage.sqlite into an unencrypted iPhone backup.

Usage:
    uv run replace_in_backup.py data/iphone_backup_restore/<device id> data/ChatStorage.new.sqlite

Overwrites WhatsApp's ChatStorage.sqlite in the backup folder and records its new size in
the backup's Manifest.db. Run it on a copy of a backup, never on your only one.
"""

import hashlib
import plistlib
import shutil
import sqlite3
import sys
from pathlib import Path

DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
RELATIVE_PATH = "ChatStorage.sqlite"


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    backup, new_file = Path(sys.argv[1]), Path(sys.argv[2])
    file_id = hashlib.sha1(f"{DOMAIN}-{RELATIVE_PATH}".encode()).hexdigest()
    target = backup / file_id[:2] / file_id
    if not target.is_file():
        sys.exit(f"{target} not found: is {backup} a device backup folder?")

    shutil.copyfile(new_file, target)
    manifest = sqlite3.connect(backup / "Manifest.db")
    (blob,) = manifest.execute("SELECT file FROM Files WHERE fileID = ?", (file_id,)).fetchone()
    record = plistlib.loads(blob)
    record["$objects"][1]["Size"] = target.stat().st_size
    manifest.execute(
        "UPDATE Files SET file = ? WHERE fileID = ?",
        (plistlib.dumps(record, fmt=plistlib.FMT_BINARY), file_id),
    )
    manifest.commit()
    manifest.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    manifest.close()
    print(f"replaced {target} ({target.stat().st_size} bytes) and updated Manifest.db")


if __name__ == "__main__":
    main()
