from __future__ import annotations

import os

from app import create_server
from service import Service
from store import Store


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    runtime_dir = os.getenv("RUNTIME_DIR", ".runtime")
    server = create_server(host, port, Service(Store(runtime_dir)))
    server.serve_forever()


if __name__ == "__main__":
    main()
