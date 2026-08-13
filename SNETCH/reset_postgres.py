"""
reset_postgres.py — One-time: wipe your LIVE/deployed Postgres database
back to a fresh, empty state (no test files, no test premium plans).

WHY THIS EXISTS
  Everything I did in the previous steps (deleting the local
  snaplock_storage/vault_storage/generated_images folders, clearing
  db_storage/snetch.db down to just the admin account) only touched the
  copy inside this sandbox — I have no network access to YOUR actual
  deployed Postgres server. This script is what YOU run, once, on the
  machine/server where DATABASE_URL in .env really points at your live
  Postgres, to do the equivalent cleanup there.

WHAT IT DOES
  - Empties `user_files`   (every tone/image/document/attachment any
                             test account ever saved via pg_storage.py)
  - Empties `premium_subscriptions` (every test payment claim/plan)
  - Leaves the TABLE STRUCTURE intact — nothing needs to be recreated,
    the app keeps working immediately, just with zero rows.
  - Does NOT touch your users table — that's in SQLite (db.py), not
    Postgres, and isn't affected by this script at all. Manage who's a
    registered user the normal way (or wipe db_storage/snetch.db on that
    same server if you also want a clean user list there).

HOW TO RUN
  1. SSH/log into the machine actually running SNETCH with your real
     DATABASE_URL in .env.
  2. From the project folder:
         python reset_postgres.py
  3. Confirm the prompt (it shows exactly how many rows will be deleted
     before doing anything).
  4. Optional but recommended right after: run `python seed_defaults.py`
     to put the default tones back for whichever users still exist.

This script refuses to run unless DATABASE_URL is set, and always asks
for a typed "yes" before deleting anything — there's no --force/silent
mode on purpose, since this is a destructive, one-way operation.
"""

import os
import sys

import pg_storage as pg


def main():
    if not pg.DATABASE_URL:
        print("DATABASE_URL is not set in your environment/.env — refusing to run.")
        print("This script only makes sense pointed at your REAL, live Postgres database.")
        sys.exit(1)

    pg.init_db()
    try:
        import premium
        premium.init_db()
        has_premium = True
    except Exception:
        has_premium = False

    with pg.db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM user_files")
        file_count = cur.fetchone()[0]

        sub_count = 0
        if has_premium:
            cur.execute("SELECT COUNT(*) FROM premium_subscriptions")
            sub_count = cur.fetchone()[0]

    print(f"About to permanently delete:")
    print(f"  - {file_count} row(s) from user_files (tones, images, vault docs, "
          f"snaplock photos/docs, whatsapp attachments)")
    print(f"  - {sub_count} row(s) from premium_subscriptions (payment claims/plans)")
    print(f"This cannot be undone.")
    answer = input("Type 'yes' to continue: ").strip().lower()
    if answer != "yes":
        print("Cancelled — nothing was deleted.")
        return

    with pg.db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM user_files")
        if has_premium:
            cur.execute("DELETE FROM premium_subscriptions")
        conn.commit()

    print("\nDone. Postgres is now empty (schema intact).")
    print("Tip: run `python seed_defaults.py` next to restore the default tones for your users.")


if __name__ == "__main__":
    main()
