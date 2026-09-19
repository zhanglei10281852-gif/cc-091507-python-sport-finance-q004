from __future__ import annotations

import os

from app import build_service, create_server


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    runtime_dir = os.getenv("RUNTIME_DIR", ".runtime")
    service = build_service(runtime_dir)
    server = create_server(host, port, service)
    server.serve_forever()


if __name__ == "__main__":
    main()
