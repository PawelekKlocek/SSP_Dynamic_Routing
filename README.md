# SSP – Dynamiczny routing zależny od obciążenia łączy

Kontroler SDN (Ryu, OpenFlow 1.3) wybierający ścieżki na podstawie aktualnego obciążenia łączy
oraz topologia testowa w Mininecie.

![Topologia](docs/topology.png)

## Co jest zrobione

- `links.json` – konfiguracja topologii: 7 switchy, 6 hostów, łącza z przepustowościami (10/15/20 Mb/s).
- `topo.py` – topologia Mininet budowana z `links.json` (łącza TCLink z limitem pasma i opóźnieniem 2 ms).
- `draw_topology.py` – rysuje topologię z `links.json` do `docs/topology.png`.
- `controller.py` – aplikacja Ryu:
  - wykrywanie topologii przez LLDP,
  - pomiar obciążenia łączy ze statystyk portów (co 2 s),
  - wagi łączy zależne od wolnego pasma i Dijkstra dla nowych przepływów,
  - przełączanie przepływu na inną ścieżkę, gdy łącze przekracza 80% zajętości,
  - proxy ARP,
  - tryb statyczny (najkrótsza ścieżka po liczbie skoków) do porównania.

## Uruchomienie

```bash
# terminal 1 – kontroler
ryu-manager --observe-links --ofp-tcp-listen-port 6633 controller.py
# tryb statyczny:
ROUTING_MODE=static ryu-manager --observe-links --ofp-tcp-listen-port 6633 controller.py

# terminal 2 – sieć
sudo python3 topo.py
```
