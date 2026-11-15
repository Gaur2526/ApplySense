import logging
import re
import traceback


TELEGRAM_TOKEN = re.compile(r"(?<![\w])\d{8,10}:[A-Za-z0-9_-]{20,}")
BOT_TOKEN_URL = re.compile(r"(/bot)\d{8,10}:[A-Za-z0-9_-]{20,}", re.IGNORECASE)
API_KEY = re.compile(r"\b(?:sk|pk|api[_-]?key)[_-]?[A-Za-z0-9_-]{16,}\b", re.IGNORECASE)


def redact_secrets(value: str) -> str:
    value = BOT_TOKEN_URL.sub(r"\1[REDACTED_TELEGRAM_TOKEN]", value)
    value = TELEGRAM_TOKEN.sub("[REDACTED_TELEGRAM_TOKEN]", value)
    return API_KEY.sub("[REDACTED_API_KEY]", value)


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_secrets(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact_secrets("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_secrets(record.exc_text)
        return True


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=getattr(logging, level.upper()), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    if not any(isinstance(item, SecretRedactionFilter) for item in root.filters):
        root.addFilter(SecretRedactionFilter())
    for handler in root.handlers:
        if not any(isinstance(item, SecretRedactionFilter) for item in handler.filters):
            handler.addFilter(SecretRedactionFilter())
