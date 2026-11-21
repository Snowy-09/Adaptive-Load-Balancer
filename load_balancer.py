
import logging

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

BACKENDS = [
    {
        "ip": "10.0.0.4",
        "mac": None,
        "port": 4,
        "tcp_port": 5001,
        "name": "S1",
    },
    {
        "ip": "10.0.0.5",
        "mac": None,
        "port": 5,
        "tcp_port": 5002,
        "name": "S2",
    },
    {
        "ip": "10.0.0.6",
        "mac": None,
        "port": 6,
        "tcp_port": 5003,
        "name": "S3",
    },
]


class LoadBalancer(app_manager.OSKenApp):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.mac_to_port = {}
        self.next_backend = 0

        # Connection-specific pending ARP resolutions.
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

        # Remove stale flows from previous controller sessions.
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
            "Static round-robin load balancer connected to switch %s",
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

        # Learn backend MAC addresses from incoming packets.
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

        # Learn backend MAC addresses from ARP replies.
        if arp_pkt.opcode == arp.ARP_REPLY:

            for backend in BACKENDS:

                if arp_pkt.src_ip == backend["ip"]:

                    backend["mac"] = arp_pkt.src_mac

                    self.logger.info(
                        "Learned %s MAC: %s",
                        backend["name"],
                        backend["mac"],
                    )

                    # Resume connections waiting for this MAC.
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

    def request_backend_mac(self, dp, backend):

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

        # Pure round-robin: no metrics or health checks.
        backend = BACKENDS[
            self.next_backend % len(BACKENDS)
        ]

        self.next_backend += 1

        self.logger.info(
            "Round-robin assigned %s:%s -> %s",
            client_ip,
            client_port,
            backend["name"],
        )

        # Resolve the backend MAC before installing flows.
        if backend["mac"] is None:

            key = (
                client_ip,
                client_port,
                VIP,
                SERVICE_PORT,
            )

            self.pending[key] = {
                "dp": dp,
                "in_port": msg.match["in_port"],
                "data": msg.data,
                "client_ip": client_ip,
                "client_mac": client_mac,
                "client_port": client_port,
                "backend": backend,
            }

            self.request_backend_mac(dp, backend)
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
