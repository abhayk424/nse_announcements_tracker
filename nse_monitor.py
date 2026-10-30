import argparse
import hashlib
import json
import logging
import sqlite3
import time

from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests


# --------------------------------------------------
# Configuration
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

NSE_PAGE_URL = (
    "https://www.nseindia.com/"
    "companies-listing/corporate-filings-application"
)

NSE_API_URL = (
    "https://www.nseindia.com/"
    "api/corporate-announcements?index=equities"
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# --------------------------------------------------
# Configuration loading
# --------------------------------------------------

def load_config():
    """Load and validate the application configuration."""

    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        config = json.load(file)

    watchlist = {
        symbol.strip().upper()
        for symbol in config.get("watchlist", [])
        if symbol.strip()
    }

    if not watchlist:
        raise ValueError(
            "The watchlist is empty. Add NSE symbols to config.json."
        )

    interval = int(config.get("poll_interval_seconds", 60))

    if interval < 30:
        raise ValueError(
            "Polling interval must be at least 30 seconds."
        )

    database_path = Path(
        config.get("database_path", "nse_announcements.db")
    )

    if not database_path.is_absolute():
        database_path = BASE_DIR / database_path

    return {
        "watchlist": watchlist,
        "interval": interval,
        "database_path": database_path,
        "baseline_on_first_run": config.get(
            "baseline_on_first_run", True
        )
    }


# --------------------------------------------------
# Database
# --------------------------------------------------

def initialize_database(database_path):
    """Create the database and announcements table."""

    database_path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with sqlite3.connect(database_path) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS announcements (
                announcement_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                company_name TEXT,
                subject TEXT,
                announcement_time TEXT,
                attachment_url TEXT,
                first_seen_at TEXT NOT NULL,
                raw_json TEXT NOT NULL
            )
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_announcements_symbol
            ON announcements(symbol)
        """)


def database_is_empty(database_path):
    """Check whether the database contains any announcements."""

    with sqlite3.connect(database_path) as connection:
        result = connection.execute(
            "SELECT COUNT(*) FROM announcements"
        ).fetchone()

    return result[0] == 0


def save_announcement(database_path, announcement):
    """
    Save an announcement only if it has not been seen before.

    Returns True for a newly inserted record.
    Returns False if the record already exists.
    """

    with sqlite3.connect(database_path) as connection:
        cursor = connection.execute("""
            INSERT OR IGNORE INTO announcements (
                announcement_id,
                symbol,
                company_name,
                subject,
                announcement_time,
                attachment_url,
                first_seen_at,
                raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            announcement["announcement_id"],
            announcement["symbol"],
            announcement["company_name"],
            announcement["subject"],
            announcement["announcement_time"],
            announcement["attachment_url"],
            datetime.now(timezone.utc).isoformat(),
            json.dumps(announcement["raw"])
        ))

        return cursor.rowcount == 1


# --------------------------------------------------
# NSE connection
# --------------------------------------------------

def create_nse_session():
    """
    Create an HTTP session and initialize it through
    the public NSE page.

    NSE may change its access requirements or endpoint.
    This function must fail clearly if retrieval fails.
    """

    session = requests.Session()

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": NSE_PAGE_URL,
        "Connection": "keep-alive"
    })

    response = session.get(
        NSE_PAGE_URL,
        timeout=20
    )

    response.raise_for_status()

    return session


def fetch_nse_announcements(session):
    """
    Fetch announcement records from NSE.

    Returns a list of dictionaries.
    Raises an exception if the endpoint fails or
    returns an unexpected response.
    """

    response = session.get(
        NSE_API_URL,
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    if not isinstance(data, list):
        raise ValueError(
            "Unexpected NSE response format. "
            "Expected a JSON list."
        )

    return data


# --------------------------------------------------
# Announcement parsing
# --------------------------------------------------

def first_nonempty(record, *keys):
    """Return the first nonempty value among candidate fields."""

    for key in keys:
        value = record.get(key)

        if value is not None and str(value).strip():
            return str(value).strip()

    return ""


def make_announcement_id(record):
    """
    Prefer the NSE sequence identifier.

    Fall back to a stable hash of available announcement
    fields if the identifier is missing.
    """

    sequence_id = first_nonempty(
        record,
        "seq_id",
        "announcementId",
        "id"
    )

    if sequence_id:
        return sequence_id

    identity_fields = [
        first_nonempty(record, "symbol"),
        first_nonempty(record, "sort_date", "an_dt", "dt"),
        first_nonempty(record, "desc", "subject"),
        first_nonempty(
            record,
            "attchmntFile",
            "attachmentUrl"
        )
    ]

    identity = "|".join(identity_fields)

    return hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()


def parse_announcement(record):
    """Normalize the NSE response into a consistent structure."""

    symbol = first_nonempty(
        record,
        "symbol",
        "ticker"
    ).upper()

    if not symbol:
        return None

    attachment = first_nonempty(
        record,
        "attchmntFile",
        "attachmentUrl",
        "attachment_url"
    )

    if attachment:
        attachment = urljoin(
            "https://www.nseindia.com",
            attachment
        )

    return {
        "announcement_id": make_announcement_id(record),
        "symbol": symbol,
        "company_name": first_nonempty(
            record,
            "sm_name",
            "companyName",
            "company_name"
        ),
        "subject": first_nonempty(
            record,
            "desc",
            "subject"
        ),
        "announcement_time": first_nonempty(
            record,
            "sort_date",
            "an_dt",
            "dt",
            "exchdisstime"
        ),
        "attachment_url": attachment,
        "raw": record
    }


# --------------------------------------------------
# Watchlist filtering and output
# --------------------------------------------------

def is_in_watchlist(announcement, watchlist):
    """Check whether an announcement belongs to a watched company."""

    return announcement["symbol"] in watchlist


def display_announcement(announcement):
    """Display a matched announcement in the terminal."""

    print("\n" + "=" * 65)
    print("NEW NSE CORPORATE ANNOUNCEMENT")
    print("=" * 65)

    print(f"Company : {announcement['company_name']}")
    print(f"Symbol  : {announcement['symbol']}")
    print(f"Subject : {announcement['subject']}")
    print(f"Time    : {announcement['announcement_time']}")

    if announcement["attachment_url"]:
        print(f"Document: {announcement['attachment_url']}")

    print("=" * 65 + "\n")


# --------------------------------------------------
# One monitoring cycle
# --------------------------------------------------

def monitor_once(
    session,
    config,
    baseline=False,
    show_existing=False
):
    """
    Fetch announcements, detect new records, persist them,
    and display records matching the watchlist.

    Baseline mode records existing announcements without
    generating alerts unless show_existing=True.
    """

    records = fetch_nse_announcements(session)

    logger.info(
        "Fetched %d announcement records from NSE.",
        len(records)
    )

    new_count = 0
    matched_count = 0

    for record in records:
        announcement = parse_announcement(record)

        if announcement is None:
            continue

        is_new = save_announcement(
            config["database_path"],
            announcement
        )

        if not is_new:
            continue

        new_count += 1

        if not is_in_watchlist(
            announcement,
            config["watchlist"]
        ):
            continue

        matched_count += 1

        if not baseline or show_existing:
            display_announcement(announcement)

    logger.info(
        "New records: %d | New watchlist matches: %d",
        new_count,
        matched_count
    )

    return new_count, matched_count


# --------------------------------------------------
# Application entry point
# --------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Monitor NSE corporate announcements."
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one monitoring cycle and exit."
    )

    parser.add_argument(
        "--show-existing",
        action="store_true",
        help=(
            "Display watchlist announcements during "
            "the initial database baseline."
        )
    )

    args = parser.parse_args()

    config = load_config()

    initialize_database(config["database_path"])

    first_run = database_is_empty(
        config["database_path"]
    )

    baseline = (
        first_run
        and config["baseline_on_first_run"]
        and not args.show_existing
    )

    logger.info(
        "Monitoring %d NSE symbols: %s",
        len(config["watchlist"]),
        ", ".join(sorted(config["watchlist"]))
    )

    if baseline:
        logger.info(
            "First run: recording existing announcements "
            "without generating notifications."
        )

    session = create_nse_session()

    try:
        while True:
            try:
                monitor_once(
                    session=session,
                    config=config,
                    baseline=baseline,
                    show_existing=args.show_existing
                )

                # Baseline behavior applies only to the
                # first successful monitoring cycle.
                baseline = False

            except (
                requests.RequestException,
                ValueError,
                sqlite3.Error
            ) as error:
                logger.exception(
                    "Monitoring cycle failed: %s",
                    error
                )

            if args.once:
                break

            time.sleep(config["interval"])

    except KeyboardInterrupt:
        logger.info("Monitoring stopped by user.")

    finally:
        session.close()


if __name__ == "__main__":
    main()
