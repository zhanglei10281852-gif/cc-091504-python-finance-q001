from __future__ import annotations

import os

from app import create_server


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    server = create_server(host, port)
    server.serve_forever()


if __name__ == "__main__":
    main()
