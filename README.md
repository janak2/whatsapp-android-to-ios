# WhatsApp Android to iOS (on Ubuntu)

Tools for moving WhatsApp chat history from an Android phone to an iPhone using only
Ubuntu, based on [residentsummer/watoi](https://github.com/residentsummer/watoi), which
needs a Mac with Xcode.

**Status:** this repo currently covers getting both WhatsApp databases onto Ubuntu and
checking that they still have the tables and columns watoi expects. watoi was written in
2017, and WhatsApp has changed its databases since. The importer itself is not ported yet
(see [Next steps](#next-steps)).

## Requirements

* Ubuntu with [uv](https://docs.astral.sh/uv/)
* Android phone with USB debugging enabled
* iPhone with WhatsApp installed and activated (no jailbreak needed)

```bash
sudo apt install adb libimobiledevice-utils sqlite3
git clone https://github.com/janak2/whatsapp-android-to-ios
cd whatsapp-android-to-ios
uv sync
mkdir -p data
```

Keep everything private (keys, backups, databases) in `data/`, which is git-ignored.

## 1. Get the Android database

WhatsApp's default local backups (`msgstore.db.crypt14`) are encrypted with a key stored
in WhatsApp's private app folder, which can only be read on a rooted phone. Switching to
end-to-end encrypted backups with a 64-digit key gives a `.crypt15` backup that can be
decrypted with that key instead.

1. In WhatsApp on Android, go to Settings > Chats > Chat backup > End-to-end encrypted
   backup, turn it on and choose **Use 64-digit encryption key instead**. A password will
   not work: password-protected backups can only be unlocked through WhatsApp's servers.
2. Write the key down somewhere safe. Without it the backups (including the Google Drive
   one) cannot be restored.
3. Tap **Back up**.
4. Save the key in a file only you can read:

   ```bash
   nano ~/wa_backup_key.txt && chmod 600 ~/wa_backup_key.txt
   ```

5. Connect the phone over USB and pull the backup:

   ```bash
   adb pull /sdcard/Android/media/com.whatsapp/WhatsApp/Databases/msgstore.db.crypt15 data/
   ```

6. Decrypt it:

   ```bash
   uv run wadecrypt "$(tr -d '[:space:]' < ~/wa_backup_key.txt)" data/msgstore.db.crypt15 data/msgstore.db
   ```

## 2. Get the iPhone database

The iPhone side works through an ordinary unencrypted local backup, which contains
WhatsApp's `ChatStorage.sqlite` as a plain SQLite file.

1. On the iPhone, send and receive a few WhatsApp messages, including one in a group.
   These rows show how current WhatsApp fills its tables, which the check in step 3 uses.
2. Connect the iPhone over USB and tap **Trust** on the phone.
3. Make sure local backups are not encrypted (this should print `false`):

   ```bash
   ideviceinfo -q com.apple.mobile.backup -k WillEncrypt
   ```

   If it prints `true`, turn encryption off with the backup password:
   `idevicebackup2 encryption off <password>`.

4. Make a full backup:

   ```bash
   idevicebackup2 backup --full data/iphone_backup
   ```

5. Copy WhatsApp's database out of the backup. The file name is the SHA-1 of
   `AppDomainGroup-group.net.whatsapp.WhatsApp.shared-ChatStorage.sqlite`:

   ```bash
   cp data/iphone_backup/*/7c/7c7fba66680ef796b916b067077cc246adacf01d data/ChatStorage.sqlite
   ```

Leave `data/iphone_backup` untouched. It is your way back if anything goes wrong.

## 3. Check the database layouts

```bash
uv run check_schema.py android data/msgstore.db
uv run check_schema.py ios data/ChatStorage.sqlite
```

Each table watoi uses is reported as `OK` or `MISSING` (with the missing columns). For
the iPhone database the script also lists columns that WhatsApp fills in every existing
row but watoi never sets; the importer will most likely need to fill those too. The
script only prints table and column names, never message contents.

To see what a database actually contains when something is missing:

```bash
sqlite3 data/msgstore.db .tables
sqlite3 data/msgstore.db "PRAGMA table_info(<table>);"
```

## Next steps

Not done yet:

* Port watoi's importer (`watoi/main.m`) to Python, writing plain SQL to
  `ChatStorage.sqlite` instead of using Apple's CoreData, adjusted to whatever step 3
  finds.
* Put the modified `ChatStorage.sqlite` back into the backup (updating its file size in
  the backup's `Manifest.db`) and restore it with `idevicebackup2 restore`. Find My iPhone
  must be off, and restoring replaces the iPhone's contents with the backup.

## Alternative

If you can reset the iPhone, WhatsApp's official transfer through Apple's
"Move to iOS" Android app moves chats and media without a computer. It only runs while
setting up a new or factory-reset iPhone.

## Credits and license

Based on [residentsummer/watoi](https://github.com/residentsummer/watoi) by Anton S.
Licensed under GPLv3, the same as watoi.
