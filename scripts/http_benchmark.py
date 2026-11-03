#!/usr/bin/env python3
"""Bounded open-loop, read-only HTTP benchmark. Never logs session credentials."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


def percentile(values, fraction):
    if not values:
        return None
    return round(sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)], 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Authorized GET endpoint; use an isolated/staging environment")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--rate", type=float, default=10)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=5)
    args = parser.parse_args()
    parsed = urllib.parse.urlparse(args.url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.query:
        parser.error("Use an HTTP(S) URL without userinfo or query credentials")
    if not (1 <= args.requests <= 10000 and 0 < args.rate <= 1000 and 1 <= args.concurrency <= 64 and 0 < args.timeout <= 30):
        parser.error("Unsupported bounded load settings")
    cookie = os.environ.get("BENCHMARK_SESSION_COOKIE")
    sem = threading.BoundedSemaphore(args.concurrency)
    results = []
    dropped = 0

    def request(scheduled):
        started = time.monotonic()
        status = "transport_error"
        try:
            headers = {"Accept": "application/json"}
            if cookie:
                headers["Cookie"] = "sessionid=" + cookie
            req = urllib.request.Request(args.url, headers=headers, method="GET")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if parsed.hostname in {"localhost", "127.0.0.1", "::1"} else urllib.request.build_opener()
            with opener.open(req, timeout=args.timeout) as response:
                response.read(1048576)
                status = str(response.status)
        except urllib.error.HTTPError as exc:
            status = str(exc.code)
        except (OSError, TimeoutError):
            pass
        finally:
            finished = time.monotonic()
            sem.release()
        return {"status": status, "service_ms": (finished - started) * 1000,
                "scheduled_to_done_ms": (finished - scheduled) * 1000,
                "scheduling_delay_ms": (started - scheduled) * 1000}

    begin = time.monotonic()
    futures = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        for index in range(args.requests):
            scheduled = begin + index / args.rate
            delay = scheduled - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if not sem.acquire(blocking=False):
                dropped += 1  # Count offered work not admitted; never hide overload.
                continue
            futures.append(executor.submit(request, scheduled))
        results = [future.result() for future in futures]
    statuses = {}
    for result in results:
        statuses[result["status"]] = statuses.get(result["status"], 0) + 1
    times = [r["scheduled_to_done_ms"] for r in results]
    print(json.dumps({"scope": "HTTP GET only, not trading latency", "offered": args.requests,
        "completed": len(results), "not_admitted": dropped, "statuses": statuses,
        "elapsed_seconds": round(time.monotonic() - begin, 3),
        "scheduled_to_done_ms": {name: percentile(times, value) for name, value in
            (("p50", .5), ("p90", .9), ("p99", .99), ("p99_9", .999), ("max", 1))},
        "max_scheduling_delay_ms": max((r["scheduling_delay_ms"] for r in results), default=None)}, indent=2))


if __name__ == "__main__":
    main()
