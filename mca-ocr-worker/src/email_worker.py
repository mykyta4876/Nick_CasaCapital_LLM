# src/email_worker.py
import time
from uw_service import process_unread_emails  # reuse the function

if __name__ == "__main__":
    while True:
        processed = process_unread_emails()
        print(f"Processed {processed} email(s)")
        time.sleep(10)