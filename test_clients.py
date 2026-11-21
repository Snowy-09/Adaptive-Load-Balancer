
import socket
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

VIP = "10.0.0.100"
PORT = 5000


def send_request(request_id):
    message = f"concurrent-request-{request_id}"
    start = time.perf_counter()

    try:
        with socket.create_connection(
            (VIP, PORT), timeout=10
        ) as sock:
            sock.settimeout(10)
            sock.sendall(message.encode())
            response = sock.recv(4096).decode().strip()

        elapsed = (time.perf_counter() - start) * 1000

        # Backend response is expected to identify S1, S2 or S3.
        server = "Unknown"
        for name in ("S1", "S2", "S3"):
            if f"Handled by {name}:" in response:
                server = name
                break

        return {
            "id": request_id,
            "server": server,
            "ms": elapsed,
            "ok": bool(response),
            "response": response,
            "error": "",
        }

    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "id": request_id,
            "server": "None",
            "ms": elapsed,
            "ok": False,
            "response": "",
            "error": str(exc),
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clients", type=int, default=15)
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()

    if args.clients < 1 or args.workers < 1:
        parser.error("--clients and --workers must be positive")

    print(
        f"Sending {args.clients} requests "
        f"with up to {args.workers} concurrent clients..."
    )

    results = []
    start = time.perf_counter()

    with ThreadPoolExecutor(
        max_workers=args.workers
    ) as pool:
        futures = [
            pool.submit(send_request, i)
            for i in range(1, args.clients + 1)
        ]

        for future in as_completed(futures):
            result = future.result()
            results.append(result)

            if result["ok"]:
                print(
                    f'Request {result["id"]:02d}: '
                    f'{result["server"]} | '
                    f'{result["ms"]:.2f} ms'
                )
            else:
                print(
                    f'Request {result["id"]:02d}: FAILED | '
                    f'{result["error"]}'
                )

    total_time = time.perf_counter() - start
    successful = [r for r in results if r["ok"]]
    failed = len(results) - len(successful)

    counts = {name: 0 for name in ("S1", "S2", "S3")}
    for result in successful:
        if result["server"] in counts:
            counts[result["server"]] += 1

    print("\n--- TEST SUMMARY ---")
    print(f"Total requests:      {len(results)}")
    print(f"Successful:          {len(successful)}")
    print(f"Failed:              {failed}")
    print(f"Elapsed time:        {total_time:.2f} seconds")
    print(f"Throughput:          {len(successful) / total_time:.2f} requests/sec")

    if successful:
        latencies = sorted(r["ms"] for r in successful)
        avg = sum(latencies) / len(latencies)
        p95_index = max(0, int(0.95 * len(latencies)) - 1)

        print(f"Average latency:     {avg:.2f} ms")
        print(f"P95 latency:         {latencies[p95_index]:.2f} ms")

    print("\nRequests per backend:")
    for name, count in counts.items():
        print(f"  {name}: {count}")


if __name__ == "__main__":
    main()
