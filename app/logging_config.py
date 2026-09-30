import logging
import logging.handlers
import os

LOGS_DIRECTORY = os.path.join(os.getcwd(), "logs")
LOG_FILE_PATH = os.path.join(LOGS_DIRECTORY, "app.log")

_configured = False


def configure_logging() -> None:
    """Wire every module's logging.getLogger(__name__) logger to a rotating file under logs/,
    plus the console. Idempotent and safe to call multiple times — streamlit_app.py reruns its
    whole script on every interaction, so this guards against adding duplicate handlers each time."""
    global _configured
    if _configured:
        return
    os.makedirs(LOGS_DIRECTORY, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE_PATH, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    _configured = True
