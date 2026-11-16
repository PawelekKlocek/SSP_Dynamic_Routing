#!/usr/bin/env python3
"""
Topologia wielosciezkowa do testow dynamicznego routingu (Mininet + Ryu).

Uruchomienie (najpierw kontroler Ryu w osobnym terminalu):
    sudo python3 topo.py
    sudo python3 topo.py --ip 127.0.0.1 --port 6633

Alternatywnie przez mn:
    sudo mn --custom topo.py --topo dynroute --link tc \
        --switch ovsk,protocols=OpenFlow13 \
        --controller=remote,ip=127.0.0.1,port=6633 --mac

Topologia (przepustowosci w Mb/s) jest zdefiniowana w links.json:
    gora:    s1 - s2 - s3 - s7   (10)
    srodek:  s1 - s4 - s7        (20)  <- najkrotsza, domyslna sciezka
    dol:     s1 - s5 - s6 - s7   (15)
    poprzeczne: s2 - s4, s4 - s6 (10)
"""
import argparse
import json
import os

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.topo import Topo

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'links.json')


def load_config(path=CONFIG_PATH):
    with open(path) as f:
        return json.load(f)


class DynamicRoutingTopo(Topo):
    """Siatka 7 switchy z trzema rozlacznymi sciezkami s1 -> s7."""

    def build(self, config=None):
        cfg = config or load_config()
        default_delay = cfg.get('default_delay', '2ms')

        for sw in cfg['switches']:
            self.addSwitch(sw, protocols='OpenFlow13')

        for h in cfg['hosts']:
            self.addHost(h['name'], ip=h['ip'])
            # lacza do hostow sa szerokie, zeby waskim gardlem byl rdzen sieci
            self.addLink(h['name'], h['switch'], bw=cfg.get('host_bw', 100))

        for link in cfg['links']:
            self.addLink(link['a'], link['b'],
                         bw=link.get('bw', cfg.get('default_bw', 10)),
                         delay=link.get('delay', default_delay))


topos = {'dynroute': (lambda: DynamicRoutingTopo())}


def run(ctrl_ip, ctrl_port):
    net = Mininet(topo=DynamicRoutingTopo(),
                  switch=OVSSwitch,
                  link=TCLink,
                  controller=None,
                  autoSetMacs=True)   # MAC 00:00:00:00:00:0X -> czytelne logi
    net.addController('c0', controller=RemoteController, ip=ctrl_ip, port=ctrl_port)
    net.start()
    info('*** Czekam chwile, az kontroler wykryje lacza (LLDP)...\n')
    info('*** Potem: pingall, iperf itd. (scenariusze w README.md)\n')
    CLI(net)
    net.stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Topologia do dynamicznego routingu')
    parser.add_argument('--ip', default='127.0.0.1', help='adres kontrolera')
    parser.add_argument('--port', type=int, default=6633, help='port kontrolera')
    args = parser.parse_args()
    setLogLevel('info')
    run(args.ip, args.port)
