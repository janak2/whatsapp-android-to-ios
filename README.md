# WhatsApp Android to iOS (on Ubuntu)

Tools for moving WhatsApp chat history from an Android phone to an iPhone using only
Ubuntu, based on [residentsummer/watoi](https://github.com/residentsummer/watoi), which
needs a Mac with Xcode.

**Status:** `import_chats.py` imports text messages between current WhatsApp databases
(Android 2.26.37 to an iPhone on iOS 26). watoi was written in 2017, and WhatsApp has
changed its databases a lot since, so this is a rewrite in Python rather than a port.
`import_media.py` then adds the photos, videos, documents, stickers and audio whose files
are still on the Android phone; media it never downloaded stays a placeholder such as
`<image>`. The text import has been restored to an iPhone, and WhatsApp kept it.

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
   nano data/wa_backup_key.txt && chmod 600 data/wa_backup_key.txt
   ```

5. Connect the phone over USB and pull the backup:

   ```bash
   adb pull /sdcard/Android/media/com.whatsapp/WhatsApp/Databases/msgstore.db.crypt15 data/
   ```

6. Decrypt it:

   ```bash
   uv run wadecrypt "$(tr -d '[:space:]' < data/wa_backup_key.txt)" data/msgstore.db.crypt15 data/msgstore.db
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

## 4. Import the Android chats

WhatsApp on the iPhone must be registered with the same number as the Android phone.
Keep the iPhone in airplane mode, with Wi-Fi off, from the backup in step 2 until the
restore in step 6, so it doesn't receive messages the backup would miss.

```bash
uv run import_chats.py data/msgstore.db data/ChatStorage.sqlite data/ChatStorage.new.sqlite
```

The script copies `ChatStorage.sqlite` to `ChatStorage.new.sqlite` and only changes the
copy. It adds every message from the Android phone's visible one-to-one and group chats
that is not already on the iPhone, skipping system notices (such as "X joined") and
message types it can't show as text. Current iPhone WhatsApp files one-to-one chats
under the contact's LID (WhatsApp's privacy ID) instead of the phone number, so the
script does the same wherever the Android database knows the LID. Every column is filled
with the values WhatsApp itself writes.

Media messages become placeholders such as `<image>` here; step 5 turns them into real
media where it can. Not moved: reactions, links between replies and the messages they
quote, and read receipts. Imported messages probably won't show up in WhatsApp's search,
which uses a separate index.

## 5. Add photos, videos and documents

Pull WhatsApp's media folder from the Android phone, then add every file that's still
there:

```bash
sudo apt install ffmpeg poppler-utils
adb pull /sdcard/Android/media/com.whatsapp/WhatsApp/Media data/android_media/
uv run import_media.py data/msgstore.db data/android_media data/ChatStorage.new.sqlite \
    data/ChatStorage.media.sqlite data/media_out
```

This copies each file into `data/media_out/Message/Media/` the way WhatsApp on the iPhone
lays them out, makes the small `.thumb` preview WhatsApp shows in chats (with `ffmpeg`
for videos and `pdftoppm` for PDFs), and turns its placeholder into a media message.
Media the Android phone never downloaded, or has since deleted, keeps its placeholder.

## 6. Put the database back and restore

Work on a copy of the backup, so `data/iphone_backup` stays as your way back:

```bash
cp -a data/iphone_backup data/iphone_backup_restore
uv run replace_in_backup.py data/iphone_backup_restore/<device id> data/ChatStorage.media.sqlite data/media_out
```

Leave out `data/media_out` (and use `data/ChatStorage.new.sqlite`) if you skipped step 5.

Turn off Find My iPhone (Settings > your name > Find My; if Stolen Device Protection
blocks this, turn it off first under Settings > Face ID & Passcode), then restore:

```bash
idevicebackup2 restore --system --settings --remove data/iphone_backup_restore
```

This replaces everything on the iPhone with the backup, like restoring from Finder on a
Mac: anything added since the backup, and data that apps keep out of backups, is lost.
The iPhone reboots when it's done. Backups don't contain the apps themselves, so the apps
then show "Waiting…" until you turn Wi-Fi on and they download again; don't delete them
meanwhile, as that deletes their restored data. WhatsApp asks you to log in again and
keeps the imported history.

## Alternative

If you can reset the iPhone, WhatsApp's official transfer through Apple's
"Move to iOS" Android app moves chats and media without a computer. It only runs while
setting up a new or factory-reset iPhone.

## Credits and license

Based on [residentsummer/watoi](https://github.com/residentsummer/watoi) by Anton S.
Licensed under GPLv3, the same as watoi.
