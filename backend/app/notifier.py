import json
import logging
import urllib.request

from .config import settings

log = logging.getLogger("andro-cd.notifier")


def notify(text: str) -> None:
    """Send a Slack (or compatible) webhook message. Never raises."""
    if not settings.slack_webhook_url:
        return
    try:
        req = urllib.request.Request(
            settings.slack_webhook_url,
            data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        log.warning("notification failed: %s", e)
