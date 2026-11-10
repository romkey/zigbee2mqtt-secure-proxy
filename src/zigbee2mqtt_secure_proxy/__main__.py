"""Command line: `zigbee2mqtt-secure-proxy` or `python -m zigbee2mqtt_secure_proxy`."""

import argparse
import asyncio
import logging
import re
import signal
import sys
from collections.abc import Sequence
from types import FrameType

import uvicorn
from sqlalchemy.exc import OperationalError

from zigbee2mqtt_secure_proxy import __version__, db
from zigbee2mqtt_secure_proxy.actions import Proxy
from zigbee2mqtt_secure_proxy.app import create_app
from zigbee2mqtt_secure_proxy.config import ConfigError, Settings
from zigbee2mqtt_secure_proxy.mqtt import MqttBridge, aiomqtt_connector

logger = logging.getLogger("zigbee2mqtt_secure_proxy")

_TOKEN_IN_URL = re.compile(r"token=[^&\s\"]+")


class RedactTokens(logging.Filter):
    """uvicorn logs WebSocket URLs, and clients put the API key in the URL."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "token=" in message:
            record.msg = _TOKEN_IN_URL.sub("token=***", message)
            record.args = None
        return True


def serve(settings: Settings) -> int:
    # log_config=None keeps uvicorn on the logging set up in main(). The access
    # log is off because request URLs can contain API keys.
    logging.getLogger("uvicorn.error").addFilter(RedactTokens())
    config = uvicorn.Config(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=5,
    )

    # uvicorn shuts down gracefully on SIGTERM, then re-raises the signal to
    # the handler that was installed before it started. Without one, that
    # would kill the process before main() could return its exit code.
    # https://github.com/encode/uvicorn/blob/master/uvicorn/server.py
    def stopped(signum: int, frame: FrameType | None) -> None:
        logger.info("received signal %d, stopped", signum)

    signal.signal(signal.SIGTERM, stopped)

    logger.info(
        "zigbee2mqtt-secure-proxy %s listening on %s:%d",
        __version__,
        settings.host,
        settings.port,
    )
    uvicorn.Server(config).run()
    return 0


def migrate(settings: Settings) -> int:
    try:
        db.migrate(settings.database_url)
    except OperationalError as e:
        # Only the exception type: the message can include connection details.
        logger.error("cannot reach the database: %s", type(e).__name__)
        return 1
    logger.info("database is at revision %s", db.head_revision())
    return 0


def prune(settings: Settings) -> int:
    async def run() -> None:
        engine = db.make_async_engine(settings.database_url)
        try:
            # Pruning never touches MQTT, so the bridge is never started.
            bridge = MqttBridge(aiomqtt_connector(settings), settings.mqtt_base_topic)
            proxy = Proxy(db.make_sessions(engine), bridge, settings)
            await proxy.expire()
            await proxy.prune()
        finally:
            await engine.dispose()

    asyncio.run(run())
    logger.info("pruned the action log")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zigbee2mqtt-secure-proxy", description=__doc__
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["serve", "migrate", "prune"],
        default="serve",
        help=(
            "serve the proxy (default), bring the database schema up to date, "
            "or delete log entries past the retention period"
        ),
    )
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
    except ConfigError as e:
        parser.exit(2, f"zigbee2mqtt-secure-proxy: {e}\n")

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    commands = {"serve": serve, "migrate": migrate, "prune": prune}
    return commands[args.command](settings)


if __name__ == "__main__":
    sys.exit(main())
