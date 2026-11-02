"""Starts the backend.

Nothing in here but wiring: the application is cdisc_backend.app.build_app, so
a WSGI-style deployment (gunicorn with the aiohttp worker, say) can import that
and skip this file entirely.
"""
import logging

from aiohttp import web

from cdisc_backend.app import build_app
from cdisc_backend.config import Config


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = Config.load()
    context = config.ssl_context

    logging.getLogger("cdisc").info(
        "listening on %s://%s:%d",
        "https" if context else "http", config.host, config.port)

    web.run_app(build_app(config), host=config.host, port=config.port,
                ssl_context=context)


if __name__ == "__main__":
    main()
