# HANDOFF BRIEF — Wavr como cérebro de presença+privacidade SOBRE o Home Assistant

> **Para:** o terminal do **desktop/core** do Wavr (dono: `sensor-fusion-architect` + `python-backend-engineer`).
> **De:** terminal do MCP (2026-07-06). **Status:** ideia validada, NÃO iniciada. Handoff — vocês decidem quando/como.
> **NÃO é do MCP nem do mobile** — eles CONSOMEM o resultado de graça. Isto é core/fusão.

## A ideia (a alavanca)

O HA já integra ~2000+ dispositivos (Zigbee, Z-Wave, câmeras, fechaduras, sensores de movimento/porta,
device_tracker, energia...). O Wavr **já lê tudo isso** via `ha_client.py`/`ha_import` (é cliente do HA do
usuário, local-only). **O gap:** LER os entities do HA ≠ FUNDIR eles na presença. Hoje a fusão do Wavr usa
sinais próprios (mmWave, BLE, rede, câmera). Este brief é sobre **fazer os sinais relevantes do HA entrarem
no modelo de presença-com-confiança do Wavr** — aí o Wavr "suporta presença" de tudo que o HA suporta, sem
reconstruir integração de device nenhuma.

## O que fundir (concreto, do sinal mais forte pro mais fraco)

| Entity HA | Vira que sinal de presença | Peso/nota |
|---|---|---|
| `binary_sensor` device_class **motion/occupancy/presence** | presença forte no cômodo | alto; decai rápido (movimento é instantâneo) |
| `device_tracker` / `person` (home/away) | presença ligada a IDENTIDADE | alto p/ casa; cuidado com PII (ver invariantes) |
| `binary_sensor` **door/window** | transição/contexto (entrou/saiu do cômodo) | médio; sinal de evento, não de estado |
| `media_player` (playing) | atividade → alguém no cômodo | médio |
| `light`/`switch` ligado | ocupação fraca (alguém acendeu) | baixo; decai devagar |

Cada um entra no `RoomState` com um **peso de confiança + decay**, reusando a matemática `colorFor(pct)` /
consenso que a ring/map-tint/room-rail já compartilham (sensor-fusion é dono disso).

## Por onde começar (fatia 1 de-riscada)

**`binary_sensor` motion/occupancy → presença**, ponta-a-ponta, UM tipo de sinal:
HA entity → mapear ao cômodo Wavr → peso na fusão → RoomState → aparece na ring/rail.
Prova o padrão; depois expande pra os outros tipos da tabela.

## Ponte de cômodo (HA area → Wavr room)

Entities do HA têm **area**; o Wavr tem cômodos no `housemap`. Precisa mapear area↔room (o
`spatial-geometry-engineer` ajuda se precisar de geometria; senão um mapa simples nome→nome no config).

## Invariantes do Wavr (NÃO violar)

- **Local-only, zero egress** — só o HA do usuário na LAN (já é assim no `ha_client`).
- **Privacidade** — a curadoria que o MCP já faz (tira vitals/targets/identities) tem que valer na fusão
  também; `device_tracker`/`person` traz identidade → tratar com o mesmo cuidado.
- **Precedência do `recog.py`** — decidir ONDE os sinais do HA entram na precedência
  (user-pin > self-describe > MUD > DHCP-fp > port-hint > OUI). Um motion-sensor do HA é um sinal de
  PRESENÇA, não de identidade de device — pode ser um eixo separado do consenso, não da precedência de recog.
- **Read-only default (`ha_import`)** — nada disto ATUA. Controle segue via `call_ha_service`, gated,
  ADR-0005 intocado. Isto é 100% READ/fusão.

## Fora de escopo (não confundir)

- Controle/atuação (é o `call_ha_service`, gated — outro assunto).
- Exposição via MCP (JÁ feita — o MCP proxya o RoomState; quando vocês enriquecerem, o MCP herda de graça).
- Display no mobile (consumidor — herda de graça também).

## Coordenação (3 terminais no mesmo repo `C:\IA\wavr`)

- Isto é **core** → branch própria de vocês (ex. `feat/ha-fusion`). Toca `fusion.py`, `recog.py`,
  `ha_client.py`/`ha_import.py`, `config.py`, `housemap.py`.
- O MCP está em `feat/mcp-http-transport` (toca `mcp*.py`, `app.py`, connectors). **Overlap pequeno**
  (`config.py`/`app.py` podem colidir — alinhar no merge).
- O mobile só consome o RoomState.

## Dono sugerido

`sensor-fusion-architect` desenha (é a IP de presença do Wavr) → `python-backend-engineer` implementa →
`qa-test-engineer` + `privacy-compliance-license-auditor` no gate (PII/local-only). Adversarial:
`surveillance-threat-modeler` (spoof de presença via HA falso).
