from vector_lake.runtime_environment import configure_numeric_threads

configure_numeric_threads()

from vector_lake.watchdog_app import start_watchdog


if __name__ == "__main__":
    start_watchdog()
