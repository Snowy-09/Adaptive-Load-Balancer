
import json
import time
from pathlib import Path

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import (
    CONFIG_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib.packet import (
    packet,
    ethernet,
    arp,
    ipv4,
    tcp,
    ether_types,
)


VIP = "10.0.0.100"
SERVICE_PORT = 5000
VIRTUAL_MAC = "02:00:00:00:00:64"

CLIENTS = {
    "10.0.0.1": 1,
    "10.0.0.2": 2,
    "10.0.0.3": 3,
}

PROJECT_DIR = Path(__file__).resolve().parent
HEALTH_FILE = PROJECT_DIR / "runtime" / "cn_sdn_backend_health.json"
HEALTH_FILE_MAX_AGE = 8

# Weighted scoring configuration.
WEIGHTS = {
    "active": 0.50,
    "latency": 0.25,
    "cpu": 0.10,
    "fairness": 0.15,
}

BACKENDS = [
    {
        "ip": "10.0.0.4",
        "mac": None,
        "port": 4,
        "tcp_port": 5001,
        "name": "S1",
        "metrics_port": 6001,
    },
    {
        "ip": "10.0.0.5",
        "mac": None,
        "port": 5,
        "tcp_port": 5002,
        "name": "S2",
        "metrics_port": 6002,
    },
    {
        "ip": "10.0.0.6",
        "mac": None,
        "port": 6,
        "tcp_port": 5003,
        "name": "S3",
        "metrics_port": 6003,
    },
]


class LoadBalancer(app_manager.OSKenApp):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.mac_to_port = {}
        self.client_macs = {}

        self.next_backend = 0
        self.connections = {}

        self.assignment_counts = {
            backend["name"]: 0
            for backend in BACKENDS
        }

        self.pending = {}
        self.arp_pending = set()

    def add_flow(
        self,
        dp,
        priority,
        match,
        actions,
        idle_timeout=60,
    ):
        parser = dp.ofproto_parser
        ofproto = dp.ofproto

        instructions = [
            parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS,
                actions,
            )
        ]

        dp.send_msg(
            parser.OFPFlowMod(
                datapath=dp,
                priority=priority,
                match=match,
                instructions=instructions,
                idle_timeout=idle_timeout,
            )
        )

    def send_packet(self, dp, in_port, actions, data):
        parser = dp.ofproto_parser
        ofproto = dp.ofproto

        dp.send_msg(
            parser.OFPPacketOut(
                datapath=dp,
                buffer_id=ofproto.OFP_NO_BUFFER,
                in_port=in_port,
                actions=actions,
                data=data,
            )
        )

    def packet_out(self, dp, msg, actions, data=None):
        self.send_packet(
            dp,
            msg.match["in_port"],
            actions,
            msg.data if data is None else data,
        )

    @set_ev_cls(
        ofp_event.EventOFPSwitchFeatures,
        CONFIG_DISPATCHER,
    )
    def switch_ready(self, ev):
        dp = ev.msg.datapath
        parser = dp.ofproto_parser
        ofproto = dp.ofproto

        # Clear stale flows from previous controller sessions.
        dp.send_msg(
            parser.OFPFlowMod(
                datapath=dp,
                command=ofproto.OFPFC_DELETE,
                table_id=ofproto.OFPTT_ALL,
                out_port=ofproto.OFPP_ANY,
                out_group=ofproto.OFPG_ANY,
                match=parser.OFPMatch(),
            )
        )

        # Send unmatched packets to the controller.
        actions = [
            parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER,
                ofproto.OFPCML_NO_BUFFER,
            )
        ]

        self.add_flow(
            dp,
            0,
            parser.OFPMatch(),
            actions,
            idle_timeout=0,
        )

        self.logger.info(
            "Adaptive load balancer connected to switch %s",
            dp.id,
        )

    @set_ev_cls(
        ofp_event.EventOFPPacketIn,
        MAIN_DISPATCHER,
    )
    def handle_packet(self, ev):
        msg = ev.msg
        dp = msg.datapath
        parser = dp.ofproto_parser
        ofproto = dp.ofproto
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth is None:
            return

        self.mac_to_port.setdefault(dp.id, {})
        self.mac_to_port[dp.id][eth.src] = in_port

        # Learn backend MAC addresses.
        for backend in BACKENDS:
            if in_port == backend["port"]:
                backend["mac"] = eth.src

        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            self.handle_arp(dp, msg, pkt)
            return

        ip = pkt.get_protocol(ipv4.ipv4)
        tcp_header = pkt.get_protocol(tcp.tcp)

        # Detect a new TCP connection to the VIP.
        if (
            ip is not None
            and tcp_header is not None
            and CLIENTS.get(ip.src) == in_port
            and ip.dst == VIP
            and tcp_header.dst_port == SERVICE_PORT
            and (tcp_header.bits & tcp.TCP_SYN) != 0
            and (tcp_header.bits & tcp.TCP_ACK) == 0
        ):
            self.handle_new_connection(
                dp,
                msg,
                ip,
                tcp_header,
                eth.src,
            )
            return

        # Normal Layer-2 forwarding.
        out_port = self.mac_to_port[dp.id].get(
            eth.dst,
            ofproto.OFPP_FLOOD,
        )

        actions = [
            parser.OFPActionOutput(out_port)
        ]

        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(
                in_port=in_port,
                eth_dst=eth.dst,
            )

            self.add_flow(
                dp,
                10,
                match,
                actions,
                idle_timeout=60,
            )

        self.packet_out(dp, msg, actions)

    def handle_arp(self, dp, msg, pkt):
        parser = dp.ofproto_parser
        ofproto = dp.ofproto

        eth = pkt.get_protocol(ethernet.ethernet)
        arp_pkt = pkt.get_protocol(arp.arp)

        if eth is None or arp_pkt is None:
            return

        if arp_pkt.opcode == arp.ARP_REPLY:

            for backend in BACKENDS:
                if arp_pkt.src_ip == backend["ip"]:

                    backend["mac"] = arp_pkt.src_mac

                    self.logger.info(
                        "Learned %s MAC: %s",
                        backend["name"],
                        backend["mac"],
                    )

                    waiting = [
                        key
                        for key, item in self.pending.items()
                        if item["backend"] is backend
                    ]

                    for key in waiting:
                        item = self.pending.pop(key)

                        self.install_connection(
                            item["dp"],
                            item["in_port"],
                            item["data"],
                            item["client_ip"],
                            item["client_mac"],
                            item["client_port"],
                            backend,
                        )

                    self.arp_pending.discard(backend["ip"])

        # Answer ARP requests for the virtual IP.
        if (
            arp_pkt.opcode == arp.ARP_REQUEST
            and arp_pkt.dst_ip == VIP
        ):
            reply = packet.Packet()

            reply.add_protocol(
                ethernet.ethernet(
                    dst=eth.src,
                    src=VIRTUAL_MAC,
                    ethertype=ether_types.ETH_TYPE_ARP,
                )
            )

            reply.add_protocol(
                arp.arp(
                    hwtype=1,
                    proto=0x0800,
                    hlen=6,
                    plen=4,
                    opcode=arp.ARP_REPLY,
                    src_mac=VIRTUAL_MAC,
                    src_ip=VIP,
                    dst_mac=arp_pkt.src_mac,
                    dst_ip=arp_pkt.src_ip,
                )
            )

            reply.serialize()

            self.packet_out(
                dp,
                msg,
                [
                    parser.OFPActionOutput(
                        ofproto.OFPP_IN_PORT
                    )
                ],
                reply.data,
            )

            return

        self.packet_out(
            dp,
            msg,
            [
                parser.OFPActionOutput(
                    ofproto.OFPP_FLOOD
                )
            ],
        )

    def request_backend_mac(self, dp, msg, backend):
        if backend["ip"] in self.arp_pending:
            return

        parser = dp.ofproto_parser

        request = packet.Packet()

        request.add_protocol(
            ethernet.ethernet(
                dst="ff:ff:ff:ff:ff:ff",
                src=VIRTUAL_MAC,
                ethertype=ether_types.ETH_TYPE_ARP,
            )
        )

        request.add_protocol(
            arp.arp(
                hwtype=1,
                proto=0x0800,
                hlen=6,
                plen=4,
                opcode=arp.ARP_REQUEST,
                src_mac=VIRTUAL_MAC,
                src_ip=VIP,
                dst_mac="00:00:00:00:00:00",
                dst_ip=backend["ip"],
            )
        )

        request.serialize()

        self.send_packet(
            dp,
            dp.ofproto.OFPP_CONTROLLER,
            [
                parser.OFPActionOutput(
                    backend["port"]
                )
            ],
            request.data,
        )

        self.arp_pending.add(backend["ip"])

        self.logger.info(
            "Resolving MAC for %s (%s)",
            backend["name"],
            backend["ip"],
        )

    def read_health_data(self):
        """Read and validate the latest health and metrics snapshot."""
        try:
            with open(
                HEALTH_FILE,
                "r",
                encoding="utf-8",
            ) as health_file:
                health_data = json.load(health_file)

            timestamp = float(health_data["timestamp"])

            age = time.time() - timestamp

            if age < 0 or age > HEALTH_FILE_MAX_AGE:
                self.logger.warning(
                    "Health file is stale (age %.2fs)",
                    age,
                )
                return None

            return health_data

        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
        ) as error:
            self.logger.warning(
                "Cannot read health file: %s",
                error,
            )
            return None

    def get_healthy_backends(self, health_data):
        """Return only backends marked HEALTHY by monitor.py."""
        if health_data is None:
            # Preserve the existing fallback behaviour.
            return BACKENDS

        statuses = health_data.get("servers", {})

        healthy = [
            backend
            for backend in BACKENDS
            if statuses.get(backend["name"]) == "HEALTHY"
        ]

        self.logger.info(
            "Healthy backends: %s",
            [backend["name"] for backend in healthy],
        )

        return healthy

    @staticmethod
    def normalize(values):
        """
        Normalize non-negative values to the range 0..1.
        If all values are zero, every normalized value is zero.
        """
        maximum = max(values.values(), default=0)

        if maximum <= 0:
            return {
                name: 0.0
                for name in values
            }

        return {
            name: max(0.0, value) / maximum
            for name, value in values.items()
        }

    def select_adaptive_backend(self):
        """Choose the backend with the lowest weighted load score."""

        health_data = self.read_health_data()
        healthy = self.get_healthy_backends(health_data)

        if not healthy:
            self.logger.error(
                "No healthy backends available"
            )
            return None

        metrics = (
            health_data.get("metrics", {})
            if health_data is not None
            else {}
        )

        # Use round-robin only if usable metrics are unavailable.
        if not metrics or any(
            backend["name"] not in metrics
            for backend in healthy
        ):
            backend = healthy[
                self.next_backend % len(healthy)
            ]

            self.next_backend = (
                BACKENDS.index(backend) + 1
            ) % len(BACKENDS)

            self.assignment_counts[backend["name"]] += 1

            self.logger.warning(
                "Metrics unavailable; round-robin fallback selected %s",
                backend["name"],
            )

            return backend

        active_values = {}
        latency_values = {}
        cpu_values = {}
        fairness_values = {}

        try:
            for backend in healthy:
                name = backend["name"]
                values = metrics[name]

                active_values[name] = max(
                    0.0,
                    float(values["active_connections"]),
                )

                latency_values[name] = max(
                    0.0,
                    float(values["average_processing_ms"]),
                )

                cpu_values[name] = max(
                    0.0,
                    float(values["cpu_utilization_percent"]),
                )

                fairness_values[name] = float(
                    self.assignment_counts[name]
                )

                if not all(
                    value < float("inf")
                    for value in (
                        active_values[name],
                        latency_values[name],
                        cpu_values[name],
                        fairness_values[name],
                    )
                ):
                    raise ValueError(
                        "Invalid or infinite metric"
                    )

        except (
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            self.logger.warning(
                "Invalid backend metrics (%s); using round-robin fallback",
                error,
            )

            backend = healthy[
                self.next_backend % len(healthy)
            ]

            self.next_backend = (
                BACKENDS.index(backend) + 1
            ) % len(BACKENDS)

            self.assignment_counts[backend["name"]] += 1

            return backend

        # Normalize each metric so that different units are comparable.
        active_normalized = self.normalize(active_values)
        latency_normalized = self.normalize(latency_values)
        cpu_normalized = self.normalize(cpu_values)
        fairness_normalized = self.normalize(fairness_values)

        scores = {}

        for backend in healthy:
            name = backend["name"]

            score = (
                WEIGHTS["active"] * active_normalized[name]
                + WEIGHTS["latency"] * latency_normalized[name]
                + WEIGHTS["cpu"] * cpu_normalized[name]
                + WEIGHTS["fairness"] * fairness_normalized[name]
            )

            scores[name] = score

        # Rotate the tie-break order to avoid fixed preference on exact ties.
        rotation = self.next_backend % len(BACKENDS)

        order = {
            BACKENDS[(rotation + index) % len(BACKENDS)]["name"]: index
            for index in range(len(BACKENDS))
        }

        backend = min(
            healthy,
            key=lambda item: (
                scores[item["name"]],
                order[item["name"]],
            ),
        )

        selected_name = backend["name"]

        # Update assignment count after selection.
        self.assignment_counts[selected_name] += 1

        self.next_backend = (
            BACKENDS.index(backend) + 1
        ) % len(BACKENDS)

        self.logger.info(
            "Weighted adaptive selection: %s | "
            "score=%.4f | active=%.3f | latency=%.3f | "
            "cpu=%.3f | fairness=%.3f | "
            "assignments=%s",
            selected_name,
            scores[selected_name],
            active_normalized[selected_name],
            latency_normalized[selected_name],
            cpu_normalized[selected_name],
            fairness_normalized[selected_name],
            self.assignment_counts,
        )

        return backend

    def handle_new_connection(
        self,
        dp,
        msg,
        ip,
        tcp_header,
        client_mac,
    ):
        client_ip = ip.src
        client_port = tcp_header.src_port

        key = (
            client_ip,
            client_port,
            VIP,
            SERVICE_PORT,
        )

        self.client_macs[client_ip] = client_mac

        # Retransmitted SYNs retain their original backend assignment.
        if key in self.connections:
            backend = self.connections[key]

        else:
            backend = self.select_adaptive_backend()

            if backend is None:
                self.logger.warning(
                    "Dropping SYN from %s:%s: no healthy backend",
                    client_ip,
                    client_port,
                )
                return

            self.connections[key] = backend

        if backend["mac"] is None:
            self.pending[key] = {
                "dp": dp,
                "in_port": msg.match["in_port"],
                "data": msg.data,
                "client_ip": client_ip,
                "client_mac": client_mac,
                "client_port": client_port,
                "backend": backend,
            }

            self.request_backend_mac(
                dp,
                msg,
                backend,
            )
            return

        self.install_connection(
            dp,
            msg.match["in_port"],
            msg.data,
            client_ip,
            client_mac,
            client_port,
            backend,
        )

    def install_connection(
        self,
        dp,
        in_port,
        data,
        client_ip,
        client_mac,
        client_port,
        backend,
    ):
        parser = dp.ofproto_parser

        # Client -> selected backend.
        forward_actions = [
            parser.OFPActionSetField(
                eth_dst=backend["mac"]
            ),
            parser.OFPActionSetField(
                ipv4_dst=backend["ip"]
            ),
            parser.OFPActionSetField(
                tcp_dst=backend["tcp_port"]
            ),
            parser.OFPActionOutput(
                backend["port"]
            ),
        ]

        forward_match = parser.OFPMatch(
            in_port=in_port,
            eth_type=ether_types.ETH_TYPE_IP,
            ip_proto=6,
            ipv4_src=client_ip,
            ipv4_dst=VIP,
            tcp_src=client_port,
            tcp_dst=SERVICE_PORT,
        )

        self.add_flow(
            dp,
            100,
            forward_match,
            forward_actions,
        )

        # Backend -> client, rewriting source back to VIP.
        reverse_actions = [
            parser.OFPActionSetField(
                eth_src=VIRTUAL_MAC
            ),
            parser.OFPActionSetField(
                eth_dst=client_mac
            ),
            parser.OFPActionSetField(
                ipv4_src=VIP
            ),
            parser.OFPActionSetField(
                tcp_src=SERVICE_PORT
            ),
            parser.OFPActionOutput(
                CLIENTS[client_ip]
            ),
        ]

        reverse_match = parser.OFPMatch(
            in_port=backend["port"],
            eth_type=ether_types.ETH_TYPE_IP,
            ip_proto=6,
            ipv4_src=backend["ip"],
            ipv4_dst=client_ip,
            tcp_src=backend["tcp_port"],
            tcp_dst=client_port,
        )

        self.add_flow(
            dp,
            100,
            reverse_match,
            reverse_actions,
        )

        self.logger.info(
            "Installed connection: %s:%s -> %s",
            client_ip,
            client_port,
            backend["name"],
        )

        # Forward the initial SYN immediately.
        self.send_packet(
            dp,
            in_port,
            forward_actions,
            data,
        )
