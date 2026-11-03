from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib.packet import packet, ethernet, ipv4, udp, ether_types
from ryu.ofproto import ofproto_v1_3

# UDP destination ports treated as emergency class
EMERGENCY_PORTS = {5005: "EMERGENCY notify", 5006: "EMERGENCY ack"}

PRIO_MISS = 0
PRIO_NORMAL = 10
PRIO_TRAP = 100
PRIO_EMERGENCY = 200
IDLE_TIMEOUT = 30   # seconds; flows expire so the trap fires again later


class EmergencyController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = {}      # dpid -> {mac: port}

    # ---------- helpers ----------
    def add_flow(self, dp, priority, match, actions, idle_timeout=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority,
                                      match=match, instructions=inst,
                                      idle_timeout=idle_timeout))

    # ---------- switch connects: install table-miss + traps ----------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_switch_connect(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        to_ctrl = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                          ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, PRIO_MISS, parser.OFPMatch(), to_ctrl)
        for port in EMERGENCY_PORTS:
            match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                    ip_proto=17, udp_dst=port)
            self.add_flow(dp, PRIO_TRAP, match, to_ctrl)
        self.logger.info("switch s%s connected: table-miss + %d emergency traps",
                         dp.id, len(EMERGENCY_PORTS))

    # ---------- packet-in: learn, classify, forward ----------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def on_packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        src, dst = eth.src, eth.dst
        table = self.mac_to_port.setdefault(dp.id, {})
        table[src] = in_port
        out_port = table.get(dst, ofp.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]

        u = pkt.get_protocol(udp.udp)
        ip = pkt.get_protocol(ipv4.ipv4)
        is_emergency = u is not None and u.dst_port in EMERGENCY_PORTS

        if out_port != ofp.OFPP_FLOOD:       # destination known -> install a flow
            if is_emergency:
                match = parser.OFPMatch(eth_dst=dst,
                                        eth_type=ether_types.ETH_TYPE_IP,
                                        ip_proto=17, udp_dst=u.dst_port)
                self.add_flow(dp, PRIO_EMERGENCY, match, actions, IDLE_TIMEOUT)
                self.logger.info(
                    "s%s CLASSIFIED %s: %s -> %s udp/%d, installed prio-%d flow, out port %s",
                    dp.id, EMERGENCY_PORTS[u.dst_port], ip.src, ip.dst,
                    u.dst_port, PRIO_EMERGENCY, out_port)
            else:
                match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
                self.add_flow(dp, PRIO_NORMAL, match, actions, IDLE_TIMEOUT)

        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                        in_port=in_port, actions=actions,
                                        data=data))
