#!/usr/bin/env python3
"""
Rysuje topologie z links.json do docs/topology.png.

    pip3 install matplotlib networkx
    python3 draw_topology.py
"""
import json
import os

import matplotlib
matplotlib.use('Agg')   # bez GUI - dziala tez przez SSH
import matplotlib.pyplot as plt
import networkx as nx

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, 'links.json')
OUT_PATH = os.path.join(HERE, 'docs', 'topology.png')


def main():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    g = nx.Graph()
    switches = cfg['switches']
    hosts = [h['name'] for h in cfg['hosts']]
    g.add_nodes_from(switches + hosts)
    for h in cfg['hosts']:
        g.add_edge(h['name'], h['switch'], bw=cfg.get('host_bw', 100), host=True)
    for link in cfg['links']:
        g.add_edge(link['a'], link['b'], bw=link.get('bw', cfg.get('default_bw', 10)), host=False)

    pos = {n: tuple(p) for n, p in cfg.get('layout', {}).items()}
    if set(pos) != set(g.nodes):
        pos = nx.spring_layout(g, seed=1)

    core = [(u, v) for u, v, d in g.edges(data=True) if not d['host']]
    access = [(u, v) for u, v, d in g.edges(data=True) if d['host']]
    widths = [1 + g[u][v]['bw'] / 5 for u, v in core]

    fig, ax = plt.subplots(figsize=(11, 6.5))
    nx.draw_networkx_edges(g, pos, edgelist=core, width=widths, edge_color='#4a6fa5', ax=ax)
    nx.draw_networkx_edges(g, pos, edgelist=access, width=1, style='dashed', edge_color='#999999', ax=ax)
    nx.draw_networkx_nodes(g, pos, nodelist=switches, node_shape='s', node_size=1300,
                           node_color='#2e4a7d', ax=ax)
    nx.draw_networkx_nodes(g, pos, nodelist=hosts, node_shape='o', node_size=900,
                           node_color='#e8a33d', ax=ax)
    nx.draw_networkx_labels(g, pos, labels={n: n for n in switches}, font_color='white',
                            font_weight='bold', ax=ax)
    nx.draw_networkx_labels(g, pos, labels={n: n for n in hosts}, font_weight='bold', ax=ax)
    nx.draw_networkx_edge_labels(g, pos, edge_labels={(u, v): '%g Mb/s' % g[u][v]['bw'] for u, v in core},
                                 font_size=9, ax=ax)

    ips = {h['name']: h['ip'].split('/')[0] for h in cfg['hosts']}
    for h, ip in ips.items():
        x, y = pos[h]
        ax.annotate(ip, (x, y), xytext=(0, -24), textcoords='offset points', ha='center', fontsize=8)

    ax.set_title('Topologia: dynamiczny routing zalezny od obciazenia (Ryu + Mininet)')
    ax.axis('off')
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    fig.tight_layout()
    fig.savefig(OUT_PATH, dpi=150)
    print('Zapisano', OUT_PATH)


if __name__ == '__main__':
    main()
