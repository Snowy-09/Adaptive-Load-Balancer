
import argparse
import collections
import concurrent.futures
import json
import os
import socket
import threading
import time
from pathlib import Path

REQUEST_FILE = Path("/home/vishnu/cn-sdn-multiclient/runtime/cn_sdn_benchmark_request.json")
RESULT_DIR = Path("/home/vishnu/cn-sdn-multiclient/runtime")
VIP = "10.0.0.100"
PORT = 5000
CLIENT_IDS = ("h1", "h2", "h3")


def atomic_write(path, payload):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_current_run_id():
    try:
        with REQUEST_FILE.open(encoding="utf-8") as f:
            return json.load(f).get("run_id")
    except (OSError, ValueError, TypeError):
        return None


def run_benchmark(job, client_id):
    total = max(1, min(500, int(job["total"])))
    workers = max(1, min(50, int(job.get("workers", 10))))
    run_id = job["run_id"]

    results = []
    cancelled = threading.Event()
    active_sockets = set()
    socket_lock = threading.Lock()

    def watch_for_cancellation():
        while not cancelled.is_set():
            if read_current_run_id() != run_id:
                cancelled.set()
                with socket_lock:
                    sockets = list(active_sockets)
                for sock in sockets:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        sock.close()
                    except OSError:
                        pass
                return
            time.sleep(0.1)

    watcher = threading.Thread(target=watch_for_cancellation, daemon=True)
    watcher.start()

    def send_request(i):
        if cancelled.is_set():
            return {"ok": False, "backend": "None",
                    "latency_ms": 0, "error": "Cancelled"}

        start = time.perf_counter()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        with socket_lock:
            if cancelled.is_set():
                sock.close()
                return {"ok": False, "backend": "None",
                        "latency_ms": 0, "error": "Cancelled"}
            active_sockets.add(sock)

        try:
            sock.settimeout(12)
            sock.connect((VIP, PORT))

            if cancelled.is_set():
                raise OSError("Benchmark cancelled")

            sock.sendall(f"{client_id}-Request-{i}".encode())
            response = b""

            while not response.endswith(b"\n") and len(response) < 4096:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk

            if cancelled.is_set():
                raise OSError("Benchmark cancelled")

            message = response.decode(errors="replace").strip()
            ok = message.startswith("Handled by ")
            backend = (
                message.split(":", 1)[0].replace("Handled by ", "")
                if ok else "None"
            )

            return {
                "ok": ok,
                "backend": backend,
                "latency_ms": (time.perf_counter() - start) * 1000,
                "error": None if ok else "Empty or invalid response"
            }

        except Exception as exc:
            return {
                "ok": False,
                "backend": "None",
                "latency_ms": (time.perf_counter() - start) * 1000,
                "error": "Cancelled" if cancelled.is_set() else str(exc)
            }

        finally:
            with socket_lock:
                active_sockets.discard(sock)
            try:
                sock.close()
            except OSError:
                pass

    wall_started_at = time.time()
    started = time.perf_counter()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for result in pool.map(send_request, range(1, total + 1)):
                results.append(result)
                if cancelled.is_set():
                    break
    finally:
        cancelled.set()
        watcher.join(timeout=1)

    if read_current_run_id() != run_id:
        print(f"{client_id}: benchmark {run_id} cancelled.", flush=True)
        return None

    elapsed = time.perf_counter() - started
    good = [r for r in results if r["ok"]]
    latencies = sorted(r["latency_ms"] for r in good)
    distribution = collections.Counter(r["backend"] for r in good)
    p95 = latencies[max(0, int(0.95 * len(latencies)) - 1)] if latencies else 0

    return {
        "run_id": run_id,
        "client_id": client_id,
        "total": total,
        "workers": workers,
        "successful": len(good),
        "failed": total - len(good),
        "elapsed_seconds": round(elapsed, 4),
        "throughput": round(len(good) / elapsed, 2) if elapsed else 0,
        "started_at": wall_started_at,
        "latencies": latencies,
        "distribution": dict(distribution),
        "errors": [r["error"] for r in results if r["error"]][:5],
        "finished_at": time.time()
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-id", choices=CLIENT_IDS, required=True)
    args = parser.parse_args()
    client_id = args.client_id

    result_file = RESULT_DIR / f"cn_sdn_benchmark_result_{client_id}.json"
    heartbeat_file = RESULT_DIR / f"cn_sdn_agent_{client_id}_heartbeat"

    def heartbeat_loop():
        while True:
            try:
                heartbeat_file.write_text(str(time.time()), encoding="utf-8")
            except OSError as exc:
                print(f"{client_id}: heartbeat error: {exc}", flush=True)
            time.sleep(1)

    threading.Thread(target=heartbeat_loop, daemon=True).start()

    last_run_id = None
    print(f"Benchmark agent ready on {client_id}; waiting for jobs.", flush=True)

    while True:
        try:
            with REQUEST_FILE.open(encoding="utf-8") as f:
                job = json.load(f)

            run_id = job.get("run_id")

            if run_id and run_id != last_run_id and client_id in job.get("clients", CLIENT_IDS):
                last_run_id = run_id
                print(f"{client_id}: running benchmark {run_id}", flush=True)

                result = run_benchmark(job, client_id)

                if result is not None and read_current_run_id() == run_id:
                    atomic_write(result_file, result)
                    print(
                        f"{client_id}: {result['successful']}/{result['total']} succeeded",
                        flush=True
                    )

        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"{client_id}: waiting for valid job: {exc}", flush=True)

        time.sleep(0.2)


if __name__ == "__main__":
    main()
