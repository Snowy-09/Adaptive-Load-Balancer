const $ = id => document.getElementById(id);
const names = ["S1", "S2", "S3"];
let latestMetrics = null;
let toastTimer;

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3000);
}

function fmt(value, digits = 1) {
  const n = Number(value);
  return value !== null && value !== undefined && Number.isFinite(n) ? n.toFixed(digits) : "—";
}

async function refreshMetrics() {
  try {
    const response = await fetch("/api/metrics", {cache:"no-store"});
    if (!response.ok) throw new Error("API returned " + response.status);
    const data = await response.json();
    latestMetrics = data;
    $("connection").textContent = "● Connected";

    for (const name of names) {
      const server = data.servers?.[name] || {};
      const metrics = server.metrics || {};
      const status = $("status-" + name);
      status.textContent = server.status || "UNKNOWN";
      status.className = "status " + String(server.status || "").toLowerCase();
      $("active-" + name).textContent = metrics.active_connections ?? "—";
      $("processing-" + name).textContent = fmt(metrics.average_processing_ms);
      $("cpu-" + name).textContent = fmt(metrics.cpu_utilization_percent);
      if (data.delays_ms && document.activeElement !== $("delay-" + name)) {
        const delay = Math.min(1000, Number(data.delays_ms[name] ?? 100));
        $("delay-" + name).value = delay;
        $("value-" + name).textContent = delay;
      }
    }

    $("updated").textContent = data.timestamp
      ? "Monitor timestamp: " + new Date(Number(data.timestamp) * 1000).toLocaleTimeString()
      : "Monitor data not available yet";
  } catch (error) {
    $("connection").textContent = "● Disconnected";
    $("updated").textContent = "Could not retrieve metrics: " + error.message;
  }
}

async function loadDelays() {
  try {
    const response = await fetch("/api/delays", {cache:"no-store"});
    const data = await response.json();
    for (const name of names) {
      const delay = Math.min(1000, Number(data.delays_ms?.[name] ?? 100));
      $("delay-" + name).value = delay;
      $("value-" + name).textContent = delay;
    }
  } catch {
    toast("Could not load saved delay settings.");
  }
}

async function applyDelays() {
  const delays = {};
  for (const name of names) delays[name] = Number($("delay-" + name).value);

  try {
    const response = await fetch("/api/delays", {
      method:"PUT",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({delays_ms:delays})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Save failed");
    $("delay-message").textContent =
      "Saved: S1 " + delays.S1 + " ms · S2 " + delays.S2 +
      " ms · S3 " + delays.S3 +
      " ms. These values do not affect live servers until integration is enabled.";
    toast("Delay settings saved.");
    refreshMetrics();
  } catch (error) {
    toast(error.message);
  }
}

function recordBenchmark(kind) {
  const raw = prompt(
    "Enter measured benchmark values as JSON: throughput, average_latency, p95_latency, successful, total",
    '{"throughput":0,"average_latency":0,"p95_latency":0,"successful":50,"total":50}'
  );
  if (!raw) return;

  try {
    const r = JSON.parse(raw);
    const throughput = Number(r.throughput);
    const avg = Number(r.average_latency);
    const p95 = Number(r.p95_latency);
    const successful = Number(r.successful);
    const total = Number(r.total);
    if (![throughput, avg, p95, successful, total].every(Number.isFinite) ||
        throughput < 0 || avg < 0 || p95 < 0 || total <= 0 ||
        successful < 0 || successful > total) {
      throw new Error("Please enter valid non-negative numbers and a positive total.");
    }
    const prefix = kind.toLowerCase();
    $(prefix + "-throughput").textContent = throughput.toFixed(2);
    $(prefix + "-latency").textContent = avg.toFixed(2);
    $(prefix + "-success").textContent = (100 * successful / total).toFixed(1) + "%";
    $("throughput").textContent = throughput.toFixed(2);
    $("latency").textContent = avg.toFixed(2);
    $("p95").textContent = p95.toFixed(2);
    $("success").textContent = (100 * successful / total).toFixed(1);
    localStorage.setItem("sdn-" + prefix, JSON.stringify(r));
    toast(kind + " results saved in this browser.");
  } catch (error) {
    toast(error.message || "Invalid JSON");
  }
}

function restoreBenchmarks() {
  for (const kind of ["static", "adaptive"]) {
    try {
      const r = JSON.parse(localStorage.getItem("sdn-" + kind));
      if (!r) continue;
      $(kind + "-throughput").textContent = Number(r.throughput).toFixed(2);
      $(kind + "-latency").textContent = Number(r.average_latency).toFixed(2);
      $(kind + "-success").textContent = (100 * Number(r.successful) / Number(r.total)).toFixed(1) + "%";
    } catch {}
  }
}

$("request-count").addEventListener("input", event => {
  $("request-count-value").textContent = event.target.value;
});
$("worker-count").addEventListener("input", event => {
  $("worker-count-value").textContent = event.target.value;
});

for (const name of names) {
  $("delay-" + name).addEventListener("input", event => {
    $("value-" + name).textContent = event.target.value;
  });
}
$("apply").addEventListener("click", applyDelays);
$("reset").addEventListener("click", () => {
  for (const name of names) {
    $("delay-" + name).value = 100;
    $("value-" + name).textContent = 100;
  }
  applyDelays();
});
$("save-static").addEventListener("click", () => {
  toast("Run a benchmark with Static selected to record real results.");
});
$("save-adaptive").addEventListener("click", () => {
  toast("Run a benchmark with Adaptive selected to record real results.");
});

loadDelays();
refreshMetrics();
restoreBenchmarks();
setInterval(refreshMetrics, 2000);


// Dashboard controller switching and real Mininet benchmark controls.
async function switchController(mode) {
  const message = $("controller-message");
  message.textContent = "Switching to " + mode + "...";
  $("mode-static").disabled = true;
  $("mode-adaptive").disabled = true;
  try {
    const response = await fetch("/api/controller", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({mode})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Controller switch failed");
    message.textContent = "Active strategy: " + mode + ". Controller PID: " + data.pid;
    $("mode-static").classList.toggle("secondary", mode !== "static");
    $("mode-adaptive").classList.toggle("secondary", mode !== "adaptive");
    toast("Switched to " + mode + " controller.");
  } catch (error) {
    message.textContent = error.message;
    toast(error.message);
  } finally {
    $("mode-static").disabled = false;
    $("mode-adaptive").disabled = false;
  }
}

async function runDashboardBenchmark() {
  const button = $("run-benchmark");
  const message = $("benchmark-message");
  button.disabled = true;
  message.textContent = "Submitting benchmark to Mininet clients h1, h2 and h3...";
  try {
    const response = await fetch("/api/benchmark", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        total: Number($("request-count").value),
        workers: Number($("worker-count").value)
      })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not start benchmark");

    message.textContent = "Benchmark running...";
    let completed = false;
    for (let i = 0; i < 600; i++) {
      await new Promise(resolve => setTimeout(resolve, 500));
      const statusResponse = await fetch("/api/benchmark/status", {cache:"no-store"});
      const status = await statusResponse.json();
      if (status.state === "complete" && status.result.run_id === data.run_id) {
        const r = status.result;
        $("bench-success").textContent = r.successful + " / " + r.total;
        $("bench-failed").textContent = r.failed;
        $("bench-distribution").textContent =
          Object.entries(r.distribution || {}).map(([name, count]) => name + ": " + count).join(" · ") || "None";
        $("throughput").textContent = Number(r.throughput).toFixed(2);
        $("latency").textContent = Number(r.average_latency).toFixed(2);
        $("p95").textContent = Number(r.p95_latency).toFixed(2);
        $("success").textContent = (100 * r.successful / r.total).toFixed(1);

        // Automatically update the comparison card for the active strategy.
        const modeResponse = await fetch("/api/controller", {cache:"no-store"});
        const modeData = await modeResponse.json();
        const prefix = modeData.mode === "adaptive" ? "adaptive" : "static";
        $(prefix + "-throughput").textContent = Number(r.throughput).toFixed(2);
        $(prefix + "-latency").textContent = Number(r.average_latency).toFixed(2);
        $(prefix + "-success").textContent =
          (100 * r.successful / r.total).toFixed(1) + "%";
        localStorage.setItem("sdn-" + prefix, JSON.stringify({
          throughput: r.throughput,
          average_latency: r.average_latency,
          p95_latency: r.p95_latency,
          successful: r.successful,
          total: r.total,
          distribution: r.distribution
        }));

        message.textContent = "Complete: " + r.clients + " clients, " + r.successful + "/" + r.total +
          " requests succeeded in " + r.elapsed_seconds + " s; throughput " + r.throughput + " req/s.";
        if (typeof toast === "function") toast("Benchmark completed.");
        completed = true;
        break;
      }
      if (status.state === "running") {
        message.textContent = "Benchmark running: " + (status.clients_ready || 0) + "/3 clients finished...";
      }
      if (status.state === "idle") {
        throw new Error("Benchmark job is not visible. Check that all three request agents are running inside Mininet.");
      }
    }
    if (!completed) message.textContent = "Still running; check the Mininet request-agent output.";
  } catch (error) {
    message.textContent = error.message;
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

$("mode-static").addEventListener("click", () => switchController("static"));
$("mode-adaptive").addEventListener("click", () => switchController("adaptive"));
$("request-count").addEventListener("input", e => $("request-count-value").textContent = e.target.value);
$("worker-count").addEventListener("input", e => $("worker-count-value").textContent = e.target.value);
$("run-benchmark").addEventListener("click", runDashboardBenchmark);
