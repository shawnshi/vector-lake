import sys

from vector_lake.watchdog_app import request_watchdog_stop, start_watchdog


if __name__ == "__main__":
    if sys.argv[1:] == ["--stop"]:
        print(f"Watchdog stop requested: {request_watchdog_stop()}")
    elif sys.argv[1:]:
        raise SystemExit("usage: watchdog_sync.py [--stop]")
    else:
        start_watchdog()
