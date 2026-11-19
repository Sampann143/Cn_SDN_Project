import os
import socket
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ipv4, udp, ether_types
from ryu.ofproto import ofproto_v1_3

MODE = os.environ.get("EMERGENCY_MODE", "dynamic")   # baseline | priority | dynamic
SOCK_PATH = "/tmp/emergency_ctrl.sock"
UPLINKS = {1: 3}              # dpid -> port number of the bottleneck link
LINK_BPS = 10_000_000
HIGH, LOW, CALM_SAMPLES = 0.70, 0.30, 3
HOLD = 10                     # seconds priority stays forced after an app signal
POLL = 1
EMERGENCY_QUEUE = 1
EMERGENCY_PORTS = {5005: "EMERGENCY notify", 5006: "EMERGENCY ack"}
PRIO_MISS, PRIO_NORMAL, PRIO_TRAP, PRIO_EMERGENCY = 0, 10, 100, 200
IDLE_TIMEOUT = 30


class EmergencyControllerD3(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.dps = {}
        self.em_flows = {}            # (dpid, dst mac, udp port) -> out port
        self.last_tx = None
        self.congested = False
        self.calm = 0
        self.forced_until = 0
        self.active = (MODE == "priority")    # is the priority queue in use?
        self.logger.info("controller mode: %s", MODE)
        hub.spawn(self.monitor)
        hub.spawn(self.listen_app)

    # ---------- flow helpers ----------
    def add_flow(self, dp, priority, match, actions, idle_timeout=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority,
                                      match=match, instructions=inst,
                                      idle_timeout=idle_timeout))

    def install_emergency(self, dp, dst, udp_dst, out_port):
        parser = dp.ofproto_parser
        actions = [parser.OFPActionOutput(out_port)]
        if self.active and UPLINKS.get(dp.id) == out_port:
            actions.insert(0, parser.OFPActionSetQueue(EMERGENCY_QUEUE))
        match = parser.OFPMatch(eth_dst=dst, eth_type=ether_types.ETH_TYPE_IP,
                                ip_proto=17, udp_dst=udp_dst)
        self.add_flow(dp, PRIO_EMERGENCY, match, actions, IDLE_TIMEOUT)
        self.em_flows[(dp.id, dst, udp_dst)] = out_port
        return actions

    # ---------- dynamic behaviour ----------
    def wanted(self):
        if MODE == "baseline":
            return False
        if MODE == "priority":
            return True
        return self.congested or time.time() < self.forced_until

    def update_state(self, reason):
        want = self.wanted()
        if want == self.active:
            return
        self.active = want
        self.logger.info("PRIORITY QUEUE %s (%s)", "ON" if want else "OFF", reason)
        for (dpid, dst, udp_dst), out_port in list(self.em_flows.items()):
            dp = self.dps.get(dpid)
            if dp and UPLINKS.get(dpid) == out_port:
                self.install_emergency(dp, dst, udp_dst, out_port)

    def evaluate(self, util):
        if util > HIGH:
            if not self.congested:
                self.logger.info("CONGESTION detected: link %.0f%% busy", util * 100)
            self.congested, self.calm = True, 0
        elif util < LOW and self.congested:
            self.calm += 1
            if self.calm >= CALM_SAMPLES:
                self.congested = False
                self.logger.info("CONGESTION cleared")
        self.update_state("congestion" if self.congested else "calm")

    def monitor(self):
        while True:
            hub.sleep(POLL)
            dp = self.dps.get(1)
            if dp:
                dp.send_msg(dp.ofproto_parser.OFPPortStatsRequest(dp, 0, UPLINKS[1]))
            self.update_state("timer")      # lets the forced window expire

    def listen_app(self):
        """Receives 'EMERGENCY_START' from the server application."""
        if os.path.exists(SOCK_PATH):
            os.unlink(SOCK_PATH)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.bind(SOCK_PATH)
        os.chmod(SOCK_PATH, 0o777)
        while True:
            data = s.recv(256)
            if data.startswith(b"EMERGENCY_START"):
                self.forced_until = time.time() + HOLD
                self.update_state("app signal")

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def on_port_stats(self, ev):
        dp = ev.msg.datapath
        if dp.id not in UPLINKS:
            return
        for st in ev.msg.body:
            if st.port_no == UPLINKS[dp.id]:
                now = time.time()
                if self.last_tx is not None:
                    bps = (st.tx_bytes - self.last_tx[1]) * 8 / (now - self.last_tx[0])
                    self.evaluate(bps / LINK_BPS)
                self.last_tx = (now, st.tx_bytes)

    # ---------- switch connects ----------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_switch_connect(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        self.dps[dp.id] = dp
        to_ctrl = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
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
        if out_port != ofp.OFPP_FLOOD:
            if u is not None and u.dst_port in EMERGENCY_PORTS:
                actions = self.install_emergency(dp, dst, u.dst_port, out_port)
                self.logger.info("s%s CLASSIFIED %s: %s -> %s udp/%d, out port %s%s",
                                 dp.id, EMERGENCY_PORTS[u.dst_port], ip.src, ip.dst,
                                 u.dst_port, out_port,
                                 " (queue %d)" % EMERGENCY_QUEUE
                                 if len(actions) > 1 else "")
            else:
                match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
                self.add_flow(dp, PRIO_NORMAL, match, actions, IDLE_TIMEOUT)

        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                        in_port=in_port, actions=actions, data=data))
