"""Read-only gateway diagnostics over a serial path or serialx URL."""

import argparse
import asyncio
import json

from . import Gateway, GatewayError


async def inspect(device: str) -> None:
    gateway = await Gateway.open(device)
    try:
        print(json.dumps({"info": gateway.info, "status": await gateway.status(), "shutters": await gateway.shutters()}, indent=2))
    finally:
        await gateway.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device", help="Serial path or serialx URL, e.g. socket://host:port (one owner at a time)")
    args = parser.parse_args()
    try:
        asyncio.run(inspect(args.device))
    except (OSError, TimeoutError, ValueError, GatewayError) as exc:
        parser.exit(1, f"Gateway unavailable: {exc}\n")


if __name__ == "__main__":
    main()
