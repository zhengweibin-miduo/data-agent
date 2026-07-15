from loguru import logger

from app.core.logging import setup_logging


def main():
    setup_logging()
    logger.info("Data agent started")


if __name__ == "__main__":
    main()
