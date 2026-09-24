#!/usr/bin/env python
"""
A Redis-PROTOCOL stand-in for machines that cannot run Redis - this project's dev
machine, a Windows laptop without Docker or WSL, has none.

    python scripts/dev_redis_standin.py                 # listens on 127.0.0.1:6379
    python scripts/dev_redis_standin.py --port 6390

This is fakeredis's TCP server, NOT Redis. It is enough to run the API and one or
more `python -m app.worker` processes against each other and watch a batch move
through, and the batch tests use fakeredis in-process for the same reason. What it
is not: durable (everything is lost when it stops), persistent, or evidence that the
queue behaves under real Redis - nothing in this repository has yet been run against a
real Redis server. Use a real one (the docker-compose `redis` service) for anything
you intend to rely on.
"""

import argparse

from fakeredis import TcpFakeServer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6379)
    args = parser.parse_args()

    server = TcpFakeServer((args.host, args.port), server_type="redis")
    print(f"fakeredis stand-in listening on {args.host}:{args.port} (not real Redis; state is lost on exit)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
