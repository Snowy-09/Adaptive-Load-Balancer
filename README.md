# NetPilot SDN Load Balancer — Multi-client experiment

## Topology

| Mininet host | Role | IP | Switch port |
|---|---|---|---|
| h1 | Client 1 | 10.0.0.1 | 1 |
| h2 | Client 2 | 10.0.0.2 | 2 |
| h3 | Client 3 | 10.0.0.3 | 3 |
| h4 | Backend S1 | 10.0.0.4 | 4 |
| h5 | Backend S2 | 10.0.0.5 | 5 |
| h6 | Backend S3 | 10.0.0.6 | 6 |

VIP: `10.0.0.100:5000`. Backend TCP ports: 5001–5003. Metrics ports: 6001–6003.

## Start the lab

Use the experimental project directory in every command below.

### 1. Start Mininet

```bash
sudo mn --topo single,6 --switch ovs,protocols=OpenFlow13 --controller=remote,ip=127.0.0.1,port=6653
```

### 2. Start backend servers and agents inside Mininet

At the Mininet prompt:

```text
h4 python3 -u /home/vishnu/cn-sdn-multiclient/server.py 5001 S1 &
h5 python3 -u /home/vishnu/cn-sdn-multiclient/server.py 5002 S2 &
h6 python3 -u /home/vishnu/cn-sdn-multiclient/server.py 5003 S3 &
h1 python3 -u /home/vishnu/cn-sdn-multiclient/monitor.py > /tmp/monitor.log 2>&1 &
h1 python3 -u /home/vishnu/cn-sdn-multiclient/request_agent.py --client-id h1 > /tmp/agent-h1.log 2>&1 &
h2 python3 -u /home/vishnu/cn-sdn-multiclient/request_agent.py --client-id h2 > /tmp/agent-h2.log 2>&1 &
h3 python3 -u /home/vishnu/cn-sdn-multiclient/request_agent.py --client-id h3 > /tmp/agent-h3.log 2>&1 &
```

The Mininet hosts share the VM filesystem, so all three agents watch the same job file and write separate result files.

### 3. Start Flask dashboard (Ubuntu terminal, not Mininet prompt)

```bash
cd ~/cn-sdn-multiclient
source ~/osken-venv/bin/activate
python3 app.py
```

### 4. Start/switch controller

Open another Ubuntu terminal:

```bash
cd ~/cn-sdn-multiclient
source ~/osken-venv/bin/activate
osken-manager --verbose load_balancer.py
```

Or start `adaptive_load_balancer.py` instead. You can also switch from the dashboard. Run only one controller at a time.

## Run experiment

Dashboard defaults: 200 requests per client, 10 workers per client. This produces 600 requests and up to 30 concurrent client workers. Keep the same request/worker settings when comparing static and adaptive. Run each strategy three times if you need repeatable results.

Results include total throughput, average and P95 latency, success/failure count, and backend distribution. Throughput is aggregate successful requests divided by the slowest client run's elapsed time.

## Quick checks

From Mininet prompt:

```text
h1 ping -c 2 10.0.0.100
h2 ping -c 2 10.0.0.100
h3 ping -c 2 10.0.0.100
h4 curl -s http://10.0.0.4:6001/metrics
h5 curl -s http://10.0.0.5:6002/metrics
h6 curl -s http://10.0.0.6:6003/metrics
```

Check agent logs with `tail -f /tmp/agent-h1.log` (or h2/h3), monitor with `tail -f /tmp/monitor.log`, and controller with `tail -f /tmp/cn_sdn_controller.log`.

## Important

This is an experimental copy. Keep the baseline directory unchanged. The controller currently learns backend MAC addresses from traffic/ARP and uses the health file written by the monitor. Start servers and monitor before running benchmarks.
