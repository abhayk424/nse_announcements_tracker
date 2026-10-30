import json

from nse_monitor import (
    create_nse_session,
    fetch_nse_announcements,
    parse_announcement,
    load_config,
)


def main():
    config = load_config()

    print("Configured watchlist:")
    print(sorted(config["watchlist"]))

    session = create_nse_session()

    try:
        records = fetch_nse_announcements(session)

        print(f"\nTotal records returned by NSE: {len(records)}")

        if records:
            print("\nFirst raw announcement:")
            print(json.dumps(records[0], indent=2))

        print("\nAnnouncement symbols returned by NSE:")

        symbols = set()

        for record in records:
            announcement = parse_announcement(record)

            if announcement:
                symbols.add(announcement["symbol"])

        print(sorted(symbols))

        print("\nWatchlist symbols found in this response:")

        matches = symbols.intersection(config["watchlist"])

        if matches:
            print(sorted(matches))
        else:
            print("No watchlist matches found.")

        print("\nConfigured symbols not found in this response:")
        print(sorted(config["watchlist"] - symbols))

    finally:
        session.close()


if __name__ == "__main__":
    main()
