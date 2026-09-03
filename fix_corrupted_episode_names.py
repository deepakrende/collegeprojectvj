# Don't Remove Credit @VJ_Bots
#
# One-time fix for the "S011 25" instead of "S01 E25" bug.
#
# ROOT CAUSE:
#   database/ia_filterdb.py had:
#       re.sub(r'(e|E)([0-9])', r'1 2', input_string)
#   The replacement string 'r'1 2'' was meant to be r'\1 \2' (a
#   backreference to the matched groups). Without the backslashes,
#   Python's re.sub does NOT treat '1' and '2' as group references -
#   it inserts the literal text "1 2" wherever "E" + one digit was
#   matched. That means the actual digit right after "E" (e.g. the
#   "2" in "E25") was permanently overwritten and lost the moment
#   each file was indexed and saved to MongoDB.
#
# WHY THIS SCRIPT DELETES INSTEAD OF "FIXING" NAMES IN PLACE:
#   The lost digit cannot be recovered from the corrupted text alone -
#   it's gone. The database's file_name field never stores which
#   channel/message the file came from, so there's no way to re-fetch
#   the original name from Telegram directly from this data either.
#   The only reliable fix is: delete the corrupted record, then
#   re-run /index on the same source channel in the bot. Indexing
#   pulls the filename fresh from Telegram (which was never touched
#   by this bug) and saves it with the NOW-FIXED regex.
#
# This script never touches the actual files on Telegram - only the
# indexed database records.
#
# USAGE (run from the project root, same environment as the bot so
# all the required env vars - API_ID, DATABASE_URI, etc. - are set):
#
#   Dry run (list what WOULD be deleted, deletes nothing):
#       python3 fix_corrupted_episode_names.py
#
#   Actually delete the corrupted entries:
#       python3 fix_corrupted_episode_names.py --apply
#
# After deleting, re-run /index (as an admin, in the bot) on every
# channel that had TV episode files, so they get re-saved correctly.

import sys
from pymongo import MongoClient
from info import (
    FILE_DB_URI,
    SEC_FILE_DB_URI,
    DATABASE_NAME,
    COLLECTION_NAME,
    MULTIPLE_DATABASE,
)

# The literal artifact the old bug always inserted, verbatim, in place
# of "E" + a digit. Any file_name containing this is corrupted.
CORRUPTION_MARKER = "1 2"


def find_and_delete(col, apply_changes):
    matches = list(
        col.find({"file_name": {"$regex": CORRUPTION_MARKER}})
    )

    print(f"\n[{col.full_name}] Found {len(matches)} corrupted entries.")

    if not matches:
        return

    preview = matches[:25]
    for doc in preview:
        print(f"  - {doc.get('file_name')}")

    if len(matches) > len(preview):
        print(f"  ... and {len(matches) - len(preview)} more")

    if not apply_changes:
        print(
            f"[{col.full_name}] Dry run only - nothing deleted. "
            f"Re-run with --apply to actually delete these."
        )
        return

    ids = [doc["_id"] for doc in matches]
    result = col.delete_many({"_id": {"$in": ids}})
    print(
        f"[{col.full_name}] Deleted {result.deleted_count} "
        f"corrupted entries."
    )


def main():
    apply_changes = "--apply" in sys.argv

    if not apply_changes:
        print(
            "Running in DRY RUN mode - nothing will be deleted.\n"
            "Review the list below, then re-run with --apply "
            "to actually delete these entries.\n"
        )
    else:
        print("Running in APPLY mode - matching entries WILL be deleted.\n")

    client = MongoClient(FILE_DB_URI)
    db = client[DATABASE_NAME]
    col = db[COLLECTION_NAME]
    find_and_delete(col, apply_changes)

    if MULTIPLE_DATABASE:
        sec_client = MongoClient(SEC_FILE_DB_URI)
        sec_db = sec_client[DATABASE_NAME]
        sec_col = sec_db[COLLECTION_NAME]
        find_and_delete(sec_col, apply_changes)

    if apply_changes:
        print(
            "\nDone. Now re-run /index (as an admin, in the bot) on "
            "every channel that had TV episode files, so they get "
            "re-saved with the corrected names."
        )


if __name__ == "__main__":
    main()
