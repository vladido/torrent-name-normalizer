from collections.abc import Mapping

from .clients import ArrClient, QbitClient, UrllibTransport
from .service import NormalizerConfig


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def build_auto_add_clients(environ: Mapping[str, str], transport_factory=UrllibTransport):
    qbit = QbitClient(
        _required(environ, "QBIT_URL"),
        _required(environ, "QBIT_USERNAME"),
        _required(environ, "QBIT_PASSWORD"),
        transport_factory(),
    )
    sonarr = ArrClient(
        "sonarr",
        _required(environ, "SONARR_URL"),
        _required(environ, "SONARR_API_KEY"),
        transport_factory(),
    )
    return qbit, sonarr


def build_clients(config: NormalizerConfig, environ: Mapping[str, str], transport_factory=UrllibTransport):
    qbit = QbitClient(
        _required(environ, "QBIT_URL"),
        _required(environ, "QBIT_USERNAME"),
        _required(environ, "QBIT_PASSWORD"),
        transport_factory(),
    )
    arr_clients = {}
    for app in sorted(set(config.categories.values())):
        prefix = app.upper()
        arr_clients[app] = ArrClient(
            app,
            _required(environ, f"{prefix}_URL"),
            _required(environ, f"{prefix}_API_KEY"),
            transport_factory(),
        )
    return qbit, arr_clients
