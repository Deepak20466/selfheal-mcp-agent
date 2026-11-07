"""Trigger one seeded runtime demo route over HTTP."""

from __future__ import annotations

import argparse
import asyncio

import httpx

ROUTES = {
    "profile": "/profiles/1",
    "pagination": "/products?page=3&page_size=3",
    "quantity": "/orders/1/total?quantity=many",
}


async def _trigger(bug: str) -> int:
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8110", timeout=10) as client:
        for _ in range(40):
            try:
                health = await client.get("/healthz")
                if health.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError("runtime demo did not become healthy on port 8110")
        response = await client.get(ROUTES[bug])
        print(f"GET {ROUTES[bug]} -> HTTP {response.status_code}")
        print(response.text[:500])
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bug", choices=ROUTES, nargs="?", default="profile")
    args = parser.parse_args()
    return asyncio.run(_trigger(args.bug))


if __name__ == "__main__":
    raise SystemExit(main())
