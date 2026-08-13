"""
seed_defaults.py — One-time: pre-load default_tone.mp3 / fire_alarm.mp3
into Postgres under a fixed system key (not tied to any one user), so
they're already "in the database" for everyone, exactly as asked.

WHY THIS EXISTS
  reminder.py / alarm.py / dailytask.py now check Postgres FIRST for the
  default/system tones (under a shared system key, see SYSTEM_EMAIL
  below), and only fall back to the local reminder_tone/, alarm_tone/,
  task_tone/ folders if Postgres has nothing there yet. So:
    - Before running this script: the app still works fine, reading the
      default tones from those local folders exactly like before.
    - After running this script once: the local folders are no longer
      needed at all — you can delete reminder_tone/, alarm_tone/, and
      task_tone/ and the app will keep serving these exact same files,
      straight from Postgres.

HOW TO RUN
  On the SAME machine/server where DATABASE_URL in your .env actually
  points at your real Postgres:

      python seed_defaults.py

  Safe to re-run any time — it just overwrites the same keys with the
  same bytes.
"""

import os
import sys

import pg_storage as pg

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# A fixed, non-login "system" key — NOT a real user account. Default/system
# tones are the same for everyone, so they're stored once here instead of
# copied into every individual user's row. reminder.py/alarm.py/dailytask.py
# look under this same key when serving the default/system tone.
SYSTEM_EMAIL = "__snetch_system_defaults__"

# (local file, feature bucket in Postgres, key/filename)
DEFAULTS = [
    (os.path.join(BASE_DIR, "reminder_tone", "default_tone.mp3"), "reminder_tone_system", "default_tone.mp3"),
    (os.path.join(BASE_DIR, "alarm_tone", "default_tone.mp3"), "alarm_tone_system", "default_tone.mp3"),
    (os.path.join(BASE_DIR, "task_tone", "default_tone.mp3"), "task_tone_system", "default_tone.mp3"),
    (os.path.join(BASE_DIR, "task_tone", "fire_alarm.mp3"), "task_tone_system", "fire_alarm.mp3"),
]


def main():
    pg.init_db()

    total_writes = 0
    for local_path, feature, filename in DEFAULTS:
        if not os.path.exists(local_path):
            print(f"[skip] {local_path} not found on disk, skipping.")
            continue
        with open(local_path, "rb") as f:
            data = f.read()

        pg.save_file(SYSTEM_EMAIL, feature, key=filename, filename=filename,
                     data=data, content_type="audio/mpeg", metadata={"default": True})
        total_writes += 1
        print(f"[ok] {filename} -> Postgres feature '{feature}'.")

    print(f"\nDone. {total_writes} default file(s) written to Postgres.")
    print("You can now safely delete the local reminder_tone/, alarm_tone/, and "
          "task_tone/ folders — the app will serve these same files from "
          "Postgres instead.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nFailed: {e}")
        print("Make sure DATABASE_URL in your .env points at your real, reachable Postgres instance.")
        sys.exit(1)
