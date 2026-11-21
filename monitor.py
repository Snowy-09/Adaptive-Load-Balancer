
import json
import os
import time
import urllib.request
from pathlib import Path


# --------------------------------------------------
# Configuration
# --------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = PROJECT_DIR / "runtime"

HEALTH_FILE = RUNTIME_DIR / "cn_sdn_backend_health.json"

INTERVAL = 2
TIMEOUT = 1.5
FAILURES_BEFORE_UNHEALTHY = 2

BACKENDS = {
    "S1": {"ip": "10.0.0.4", "metrics_port": 6001},
    "S2": {"ip": "10.0.0.5", "metrics_port": 6002},
    "S3": {"ip": "10.0.0.6", "metrics_port": 6003},
}

failure_counts = {
    name: 0 for name in BACKENDS
}

statuses = {
    name: "UNKNOWN" for name in BACKENDS
}

# Keep the last successfully received metrics.
last_metrics = {}


# --------------------------------------------------
# Fetch backend metrics
# --------------------------------------------------

def fetch_metrics(name, backend):
    url = (
        f"http://{backend['ip']}:"
        f"{backend['metrics_port']}/metrics"
    )

    request = urllib.request.Request(
        url,
        headers={"Connection": "close"}
    )

    with urllib.request.urlopen(
        request, timeout=TIMEOUT
    ) as response:
        data = json.loads(response.read().decode("utf-8"))

    required = (
        "active_connections",
        "average_processing_ms",
        "total_requests",
        "successful_requests",
        "failed_requests",
        "cpu_utilization_percent",
    )

    missing = [
        field for field in required
        if field not in data
    ]

    if missing:
        raise ValueError(
            f"Missing metrics: {', '.join(missing)}"
        )

    return {
        "active_connections": int(
            data["active_connections"]
        ),
        "average_processing_ms": float(
            data["average_processing_ms"]
        ),
        "cpu_utilization_percent": float(
            data["cpu_utilization_percent"]
        ),
        "total_requests": int(
            data["total_requests"]
        ),
        "successful_requests": int(
            data["successful_requests"]
        ),
        "failed_requests": int(
            data["failed_requests"]
        ),
    }


# --------------------------------------------------
# Write health information atomically
# --------------------------------------------------

def write_health_file(servers, metrics):
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    payload = {
        "timestamp": time.time(),
        "servers": servers,
        "metrics": metrics,
    }

    temp_file = HEALTH_FILE.with_suffix(".json.tmp")

    with temp_file.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)
        file.flush()
        os.fsync(file.fileno())

    os.replace(temp_file, HEALTH_FILE)


# --------------------------------------------------
# Main monitoring loop
# --------------------------------------------------

def main():
    print("Backend monitor started.", flush=True)

    while True:
        for name, backend in BACKENDS.items():

            try:
                metrics = fetch_metrics(name, backend)

                last_metrics[name] = metrics
                failure_counts[name] = 0
                statuses[name] = "HEALTHY"

            except Exception as exc:
                failure_counts[name] += 1

                print(
                    f"{name} metrics check failed "
                    f"({failure_counts[name]}/"
                    f"{FAILURES_BEFORE_UNHEALTHY}): {exc}",
                    flush=True
                )

                if (
                    failure_counts[name]
                    >= FAILURES_BEFORE_UNHEALTHY
                ):
                    statuses[name] = "UNHEALTHY"

                # Do not replace missing metrics with zero.
                # Preserve the last known metrics, if available.

        # Only include actual previously received metrics.
        metrics_snapshot = {
            name: dict(last_metrics[name])
            for name in last_metrics
        }

        write_health_file(
            dict(statuses),
            metrics_snapshot
        )

        summary = []

        for name in BACKENDS:
            if name in last_metrics:
                metric = last_metrics[name]

                summary.append(
                    f"{name}: {statuses[name]}, "
                    f"CPU={metric['cpu_utilization_percent']:.2f}%, "
                    f"active={metric['active_connections']}, "
                    f"avg={metric['average_processing_ms']:.2f}ms"
                )
            else:
                summary.append(
                    f"{name}: {statuses[name]}, metrics unavailable"
                )

        print(" | ".join(summary), flush=True)

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
