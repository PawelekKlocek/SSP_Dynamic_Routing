# -*- coding: utf-8 -*-
"""
Dynamiczny routing zalezny od obciazenia laczy - aplikacja Ryu (OpenFlow 1.3).

Uruchomienie (tryb dynamiczny):
    ryu-manager --observe-links --ofp-tcp-listen-port 6633 controller.py

Tryb porownawczy (statyczny Dijkstra po liczbie skokow, bez przelaczania):
    ROUTING_MODE=static ryu-manager --observe-links --ofp-tcp-listen-port 6633 controller.py

Jak to dziala:
  1. Topologia   - ryu.topology (LLDP) buduje graf skierowany switchy (networkx).
  2. Telemetria  - co POLL_INTERVAL s OFPPortStatsRequest do kazdego switcha;
                   z roznicy tx_bytes liczona jest szybkosc na porcie, wygladzana EMA.
  3. Wagi        - w(l) = 1 / (C(l) - B_used(l) + eps)   (odwrotnosc pasma rezydualnego).
  4. Routing     - dla nowego przeplywu (eth_src, eth_dst) Dijkstra po wagach,
                   instalacja regul od konca sciezki do poczatku.
  5. Rebalancing - jesli waskie gardlo sciezki przeplywu > UTIL_THRESHOLD,
                   a alternatywa daje wyraznie wiecej pasma -> przelaczenie.
                   Histereza (prog + margines zysku) i czasy wstrzymania
                   chronia przed route flappingiem.
  6. ARP         - proxy ARP w kontrolerze + flood tylko na porty hostow,
                   wiec petle w topologii nie powoduja burzy broadcastowej.
"""
import json
import os
import time

import networkx as nx

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,
                                    MAIN_DISPATCHER, set_ev_cls)
from ryu.lib import hub
from ryu.lib.packet import arp, ether_types, ethernet, ipv4, packet
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event as topo_event
from ryu.topology.api import get_link, get_switch

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'links.json')
MODE = os.environ.get('ROUTING_MODE', 'dynamic').lower()

# --- parametry -------------------------------------------------------------
POLL_INTERVAL = 2.0       # s, okres odpytywania o statystyki portow
EMA_ALPHA = 0.5           # waga najnowszej probki w sredniej wykladniczej
UTIL_THRESHOLD = 0.8      # przelaczaj dopiero powyzej 80% zajetosci
GAIN_MARGIN = 0.2         # alternatywa musi miec >= 20% wiecej pasma rezydualnego...
MIN_GAIN_MBPS = 1.0       # ...i co najmniej 1 Mb/s wiecej
FLOW_COOLDOWN = 6.0       # s, minimalny czas miedzy przelaczeniami tego samego przeplywu
GLOBAL_HOLDDOWN = 6.0     # s, po kazdym przelaczeniu czekamy na swieze pomiary
DISCOVERY_DELAY = 5.0     # s, ignoruj ruch hostow, az LLDP wykryje lacza
EPS = 0.01                # Mb/s, zabezpieczenie przed dzieleniem przez zero
FLOW_IDLE_TIMEOUT = 15    # s
PRIO_FLOW = 10
OFPP_MAX = 0xffffff00     # porty powyzej to porty specjalne (LOCAL, CONTROLLER...)


class LoadAwareRouter(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dynamic = MODE != 'static'

        self.datapaths = {}       # dpid -> datapath
        self.graph = nx.DiGraph() # krawedz u->v: port (wyjscie z u), weight
        self.switch_ports = {}    # dpid -> {port_no}
        self.link_ports = set()   # {(dpid, port_no)} porty miedzy switchami

        self.hosts = {}           # mac -> (dpid, port)
        self.arp_table = {}       # ip -> mac

        self.port_bytes = {}      # (dpid, port) -> (tx_bytes, czas)
        self.port_rate = {}       # (dpid, port) -> Mb/s (EMA)

        self.flows = {}           # (src_mac, dst_mac) -> {'path': [...], 't': czas}
        self.last_reroute = 0.0
        self.first_seen = None

        self.default_bw = 10.0
        self.capacity = self._load_capacity()

        self.logger.info('Tryb routingu: %s',
                         'DYNAMICZNY (load-aware)' if self.dynamic else 'STATYCZNY (hop count)')
        self.monitor_thread = hub.spawn(self._monitor)

    # ------------------------------------------------------------------ config
    def _load_capacity(self):
        """Przepustowosci laczy z links.json (OVS na veth nie zna limitow tc)."""
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        self.default_bw = float(cfg.get('default_bw', 10))
        cap = {}
        for link in cfg['links']:
            a, b = self._dpid(link['a']), self._dpid(link['b'])
            bw = float(link.get('bw', self.default_bw))
            cap[(a, b)] = cap[(b, a)] = bw
        return cap

    @staticmethod
    def _dpid(name):
        # Mininet nadaje switchowi sN dpid = N
        return int(''.join(c for c in name if c.isdigit()))

    # --------------------------------------------------------- OpenFlow basics
    def _add_flow(self, dp, priority, match, actions, idle=0, flags=0):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                      instructions=inst, idle_timeout=idle, flags=flags))

    def _packet_out(self, dp, port, data):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                        in_port=ofp.OFPP_CONTROLLER,
                                        actions=[parser.OFPActionOutput(port)], data=data))

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features(self, ev):
        dp = ev.msg.datapath
        parser, ofp = dp.ofproto_parser, dp.ofproto
        # table-miss -> kontroler
        self._add_flow(dp, 0, parser.OFPMatch(),
                       [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])
        # IPv6 (ND/MLD z hostow Mininetu) tylko zasmieca - odrzucamy
        self._add_flow(dp, 1, parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IPV6), [])

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
            if self.first_seen is None:
                self.first_seen = time.time()
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)

    # ---------------------------------------------------------------- topology
    @set_ev_cls([topo_event.EventSwitchEnter, topo_event.EventSwitchLeave,
                 topo_event.EventLinkAdd, topo_event.EventLinkDelete])
    def _topology_changed(self, ev):
        switches = get_switch(self, None)
        links = get_link(self, None)

        g = nx.DiGraph()
        self.switch_ports = {}
        for sw in switches:
            g.add_node(sw.dp.id)
            self.switch_ports[sw.dp.id] = {p.port_no for p in sw.ports if p.port_no < OFPP_MAX}

        self.link_ports = set()
        for l in links:
            g.add_edge(l.src.dpid, l.dst.dpid, port=l.src.port_no, weight=1.0)
            self.link_ports.add((l.src.dpid, l.src.port_no))

        self.graph = g
        self._update_weights()
        self.logger.info('[TOPO] %d switchy, %d laczy', g.number_of_nodes(), g.number_of_edges() // 2)

    def _edge_ports(self, dpid):
        """Porty, do ktorych podpiete sa hosty (nie inne switche)."""
        return [p for p in self.switch_ports.get(dpid, ()) if (dpid, p) not in self.link_ports]

    # ------------------------------------------------------------- telemetry
    def _monitor(self):
        while True:
            for dp in list(self.datapaths.values()):
                dp.send_msg(dp.ofproto_parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY))
            hub.sleep(POLL_INTERVAL)
            self._update_weights()
            self._log_links()
            if self.dynamic:
                self._rebalance()

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply(self, ev):
        dpid = ev.msg.datapath.id
        for stat in ev.msg.body:
            key = (dpid, stat.port_no)
            now = stat.duration_sec + stat.duration_nsec / 1e9
            prev = self.port_bytes.get(key)
            self.port_bytes[key] = (stat.tx_bytes, now)
            if prev is None or now <= prev[1]:
                continue
            # lacza sa full-duplex, wiec dla kierunku u->v liczy sie tx na porcie wyjsciowym u
            rate = (stat.tx_bytes - prev[0]) * 8 / (now - prev[1]) / 1e6
            old = self.port_rate.get(key, rate)
            self.port_rate[key] = EMA_ALPHA * rate + (1 - EMA_ALPHA) * old

    def _capacity(self, u, v):
        return self.capacity.get((u, v), self.default_bw)

    def _used(self, u, v):
        return self.port_rate.get((u, self.graph[u][v]['port']), 0.0)

    def _residual(self, u, v):
        return max(self._capacity(u, v) - self._used(u, v), 0.0)

    def _update_weights(self):
        for u, v, data in self.graph.edges(data=True):
            data['weight'] = 1.0 / (self._residual(u, v) + EPS) if self.dynamic else 1.0

    def _log_links(self):
        busy = []
        for u, v in self.graph.edges():
            used, cap = self._used(u, v), self._capacity(u, v)
            if used > 0.5:
                busy.append('s%d->s%d %.1f/%.0f (%d%%)' % (u, v, used, cap, 100 * used / cap))
        if busy:
            self.logger.info('[LOAD] ' + ' | '.join(sorted(busy)))

    # ----------------------------------------------------------------- paths
    def _shortest_path(self, src_dpid, dst_dpid):
        try:
            return nx.shortest_path(self.graph, src_dpid, dst_dpid, weight='weight')  # Dijkstra
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

    def _path_edges(self, path):
        return list(zip(path[:-1], path[1:]))

    def _path_valid(self, path):
        return all(self.graph.has_edge(u, v) for u, v in self._path_edges(path))

    def _path_util(self, path):
        return max((self._used(u, v) / self._capacity(u, v) for u, v in self._path_edges(path)),
                   default=0.0)

    def _path_residual(self, path):
        return min((self._residual(u, v) for u, v in self._path_edges(path)),
                   default=float('inf'))

    @staticmethod
    def _fmt(path):
        return '-'.join('s%d' % d for d in path)

    def _install_path(self, src, dst, path):
        dst_port = self.hosts[dst][1]
        hops = []
        for i, dpid in enumerate(path):
            out = self.graph[dpid][path[i + 1]]['port'] if i < len(path) - 1 else dst_port
            hops.append((i, dpid, out))
        # od konca: zanim pierwszy switch zacznie wysylac, dalsze juz maja reguly
        for i, dpid, out in reversed(hops):
            dp = self.datapaths.get(dpid)
            if dp is None:
                continue
            parser = dp.ofproto_parser
            flags = dp.ofproto.OFPFF_SEND_FLOW_REM if i == 0 else 0
            self._add_flow(dp, PRIO_FLOW, parser.OFPMatch(eth_src=src, eth_dst=dst),
                           [parser.OFPActionOutput(out)], idle=FLOW_IDLE_TIMEOUT, flags=flags)

    def _route(self, src, dst, data):
        prev = self.flows.get((src, dst))
        if prev and time.time() - prev['t'] < 1.0:
            # kolejne pakiety, ktore wpadly do kontrolera, zanim reguly zadzialaly
            self._deliver(dst, data)
            return
        src_dpid = self.hosts[src][0]
        dst_dpid = self.hosts[dst][0]
        path = self._shortest_path(src_dpid, dst_dpid)
        if path is None:
            return
        self._install_path(src, dst, path)
        self.flows[(src, dst)] = {'path': path, 't': time.time()}
        if prev is None or prev['path'] != path:
            self.logger.info('[ROUTE] %s -> %s : %s', src, dst, self._fmt(path))
        self._deliver(dst, data)

    def _rebalance(self):
        now = time.time()
        if now - self.last_reroute < GLOBAL_HOLDDOWN:
            return
        for (src, dst), info in list(self.flows.items()):
            path = info['path']
            if len(path) < 2 or now - info['t'] < FLOW_COOLDOWN:
                continue
            if src not in self.hosts or dst not in self.hosts:
                continue

            valid = self._path_valid(path)
            util = self._path_util(path) if valid else 1.0
            if valid and util <= UTIL_THRESHOLD:
                continue

            new_path = self._shortest_path(path[0], path[-1])
            if not new_path or new_path == path:
                continue

            cur_res = self._path_residual(path) if valid else 0.0
            new_res = self._path_residual(new_path)
            # histereza: przelaczamy tylko przy wyraznym zysku
            if valid and (new_res < cur_res * (1 + GAIN_MARGIN) or new_res - cur_res < MIN_GAIN_MBPS):
                continue

            self._install_path(src, dst, new_path)
            info.update(path=new_path, t=now)
            self.last_reroute = now
            self.logger.info('[REROUTE] %s -> %s : %s (%.0f%%, wolne %.1f Mb/s) => %s (wolne %.1f Mb/s)',
                             src, dst, self._fmt(path), 100 * util, cur_res,
                             self._fmt(new_path), new_res)
            return   # jeden przeplyw na cykl - kolejne decyzje po swiezym pomiarze

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def _flow_removed(self, ev):
        msg = ev.msg
        if msg.reason != msg.datapath.ofproto.OFPRR_IDLE_TIMEOUT:
            return
        if 'eth_src' not in msg.match or 'eth_dst' not in msg.match:
            return
        key = (msg.match['eth_src'], msg.match['eth_dst'])
        if self.flows.pop(key, None):
            self.logger.info('[EXPIRE] %s -> %s', *key)

    # ------------------------------------------------------------ packet-in
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth is None or eth.ethertype in (ether_types.ETH_TYPE_LLDP, ether_types.ETH_TYPE_IPV6):
            return
        if self.first_seen is None or time.time() - self.first_seen < DISCOVERY_DELAY:
            return   # topologia jeszcze niekompletna - nie zgadujemy portow hostow
        if (dp.id, in_port) in self.link_ports:
            return   # obslugujemy tylko ruch wchodzacy od hostow

        src, dst = eth.src, eth.dst
        if src not in self.hosts:
            self.hosts[src] = (dp.id, in_port)
            self.logger.info('[HOST] %s @ s%d port %d', src, dp.id, in_port)

        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt:
            self.arp_table[arp_pkt.src_ip] = src
            self._handle_arp(dp, in_port, eth, arp_pkt, msg.data)
            return

        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt:
            self.arp_table[ip_pkt.src] = src

        if dst in self.hosts:
            self._route(src, dst, msg.data)
        else:
            self._flood(msg.data, dp.id, in_port)

    def _handle_arp(self, dp, in_port, eth, req, data):
        if req.opcode == arp.ARP_REQUEST and req.dst_ip in self.arp_table:
            # proxy ARP: odpowiadamy sami, bez rozsylania po sieci
            target_mac = self.arp_table[req.dst_ip]
            reply = packet.Packet()
            reply.add_protocol(ethernet.ethernet(dst=req.src_mac, src=target_mac,
                                                 ethertype=ether_types.ETH_TYPE_ARP))
            reply.add_protocol(arp.arp(opcode=arp.ARP_REPLY, src_mac=target_mac, src_ip=req.dst_ip,
                                       dst_mac=req.src_mac, dst_ip=req.src_ip))
            reply.serialize()
            self._packet_out(dp, in_port, reply.data)
        elif eth.dst in self.hosts:
            self._deliver(eth.dst, data)
        else:
            self._flood(data, dp.id, in_port)

    def _deliver(self, dst_mac, data):
        dpid, port = self.hosts[dst_mac]
        dp = self.datapaths.get(dpid)
        if dp is not None:
            self._packet_out(dp, port, data)

    def _flood(self, data, in_dpid, in_port):
        """Flood tylko na porty hostow - petle w topologii nie maja znaczenia."""
        for dpid, dp in self.datapaths.items():
            for port in self._edge_ports(dpid):
                if (dpid, port) != (in_dpid, in_port):
                    self._packet_out(dp, port, data)
