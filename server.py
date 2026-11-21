
import socket
import sys
import time
import json
import threading
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor


# --------------------------------------------------
# Configuration
# --------------------------------------------------

PORT = int(sys.argv[1])
NAME = sys.argv[2]
METRICS_PORT = PORT + 1000

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = PROJECT_DIR / "dashboard" / "config.json"

DEFAULT_DELAY_SECONDS = 0.1
MAX_WORKERS = 50
SOCKET_TIMEOUT = 10

lock = threading.Lock()

# CPU usage sampling
_last_cpu_time = time.process_time()
_last_wall_time = time.monotonic()
cpu_utilization_percent = 0.0
cpu_lock = threading.Lock()


# --------------------------------------------------
# Processing delay
# --------------------------------------------------

def get_processing_delay():
    """Read this backend's artificial delay from config.json."""
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as config_file:
            config = json.load(config_file)

        delays = config.get("delays_ms", {})
        delay_ms = float(
            delays.get(NAME, DEFAULT_DELAY_SECONDS * 1000)
        )

        delay_ms = max(0.0, min(5000.0, delay_ms))
        return delay_ms / 1000.0

    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_DELAY_SECONDS


# --------------------------------------------------
# CPU monitoring
# --------------------------------------------------

def update_cpu_usage():
    global _last_cpu_time
    global _last_wall_time
    global cpu_utilization_percent

    now_cpu = time.process_time()
    now_wall = time.monotonic()

    cpu_delta = now_cpu - _last_cpu_time
    wall_delta = now_wall - _last_wall_time

    if wall_delta > 0:
        cores = os.cpu_count() or 1
        usage = (cpu_delta / wall_delta) * 100.0 / cores
        cpu_utilization_percent = max(0.0, min(100.0, usage))

    _last_cpu_time = now_cpu
    _last_wall_time = now_wall


def get_cpu_usage():
    with cpu_lock:
        update_cpu_usage()
        return round(cpu_utilization_percent, 2)


# --------------------------------------------------
# Statistics
# --------------------------------------------------

stats = {
    "server": NAME,
    "tcp_port": PORT,
    "metrics_port": METRICS_PORT,
    "total_connections": 0,
    "total_requests": 0,
    "active_connections": 0,
    "successful_requests": 0,
    "failed_requests": 0,
    "total_processing_seconds": 0.0,
    "last_request_time": None,
}


# --------------------------------------------------
# TCP request handler
# --------------------------------------------------

def handle_client(conn, addr):
    with lock:
        stats["total_connections"] += 1
        stats["active_connections"] += 1

    request_started = False
    processing_started = None
    success = False

    try:
        with conn:
            conn.settimeout(SOCKET_TIMEOUT)

            # Receive request. Time spent waiting here is NOT
            # included in application processing latency.
            request = conn.recv(4096).decode(
                "utf-8", errors="replace"
            ).strip()

            if not request:
                return

            request_started = True

            with lock:
                stats["total_requests"] += 1
                stats["last_request_time"] = time.time()

            processing_started = time.perf_counter()

            # Simulated application processing.
            time.sleep(get_processing_delay())

            response = f"Handled by {NAME}: {request}\n"
            conn.sendall(response.encode("utf-8"))

            success = True

    except (ConnectionError, OSError, socket.timeout):
        # Intentionally no per-request terminal printing.
        # This prevents Mininet terminal output from blocking workers.
        pass

    finally:
        if processing_started is not None:
            elapsed = time.perf_counter() - processing_started
        else:
            elapsed = 0.0

        with lock:
            stats["active_connections"] = max(
                0, stats["active_connections"] - 1
            )

            if request_started:
                stats["total_processing_seconds"] += elapsed

                if success:
                    stats["successful_requests"] += 1
                else:
                    stats["failed_requests"] += 1


# --------------------------------------------------
# Metrics API
# --------------------------------------------------

def get_metrics():
    with lock:
        result = dict(stats)

    completed = (
        result["successful_requests"]
        + result["failed_requests"]
    )

    result["average_processing_ms"] = (
        result["total_processing_seconds"] / completed * 1000
        if completed else 0.0
    )

    result["cpu_utilization_percent"] = get_cpu_usage()
    result["healthy"] = True

    return result


class MetricsHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path not in ("/health", "/metrics"):
            self.send_error(404, "Not found")
            return

        body = json.dumps(get_metrics()).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()

        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Disable HTTP access logs to avoid unnecessary terminal output.
        pass


# --------------------------------------------------
# Server startup
# --------------------------------------------------

def main():
    tcp_server = socket.socket(
        socket.AF_INET, socket.SOCK_STREAM
    )

    tcp_server.setsockopt(
        socket.SOL_SOCKET, socket.SO_REUSEADDR, 1
    )

    tcp_server.bind(("0.0.0.0", PORT))
    tcp_server.listen(100)

    metrics_server = ThreadingHTTPServer(
        ("0.0.0.0", METRICS_PORT),
        MetricsHandler
    )

    metrics_server.daemon_threads = True

    metrics_thread = threading.Thread(
        target=metrics_server.serve_forever,
        daemon=True
    )
    metrics_thread.start()

    print(
        f"{NAME} TCP listening on {PORT}; "
        f"metrics listening on {METRICS_PORT}",
        flush=True
    )

    try:
        with ThreadPoolExecutor(
            max_workers=MAX_WORKERS
        ) as pool:

            while True:
                conn, addr = tcp_server.accept()
                pool.submit(handle_client, conn, addr)

    except KeyboardInterrupt:
        pass

    finally:
        tcp_server.close()
        metrics_server.shutdown()
        metrics_server.server_close()


if __name__ == "__main__":
    main()
