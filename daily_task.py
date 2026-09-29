#!/usr/bin/env python
"""PythonAnywhere scheduled task: daily security cleanup.

Tasks tab command:

    /home/calica/.virtualenvs/venv/bin/python /home/calica/LOAN_SYSTEM/daily_task.py

Schedule it once a day. It loads the project .env, then:

- deactivates staff accounts with 8 failed sign-ins in a row (keeps the last admin)
- signs out deactivated accounts
- deletes expired login sessions
- on the server, restricts .env and the database so only this account can read them
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "loan_system.settings")


def main():
    import django

    django.setup()
    from lending.security_task import run_daily_security

    return run_daily_security()


if __name__ == "__main__":
    raise SystemExit(main())
