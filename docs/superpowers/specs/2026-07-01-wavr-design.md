# Wavr — Documento de Design (Revisão 2: fusão multi-modal)

**Data:** 2026-07-01
**Autor:** Augusto Bastos
**Status:** Revisão do design (fusão-no-núcleo + câmeras) → aguardando aprovação para reescrever o plano

> **Rev 2 muda o núcleo:** de "um stream WiFi → tela" para "N modalidades → motor de fusão → estado do cômodo com confiança". Câmeras (CV local) entram como a modalidade de maior precisão. O modelo de dois planos e a interface `SensorSource` continuam — a fusão é construída *em cima* deles.

---

## 1. O que é

**Wavr** é um sistema de sensoriamento, controle e vigilância residencial que **funde várias
modalidades de detecção** num estado unificado e explicável por cômodo. Modalidades:

- **WiFi CSI** (RuView/ESP32) — presença + respiração + batimento, atravessa parede, sem vídeo.
- **Rede** (scan ARP/roteador) — "quem está em casa?" por dispositivo. Custo $0, dado real hoje.
- **Câmera + CV local** (Tapo C210/TC40 via RTSP → detecção de pessoa na RTX 3060) — a
  modalidade de **maior precisão** onde há vídeo.
- **Simulada** (multi-modal) — apartamento fictício para a vitrine pública ($0, sem dado real).
- *(próximas: BLE, mmWave — mesma interface.)*

O coração é o **FusionEngine**: combina as leituras das modalidades presentes em cada cômodo
num `RoomState` com confiança 0..1 e a explicação de *por quê* ("rede: vazio · wifi: respiração
fraca → 72% ocupado, provável dormindo").

### Objetivo — um projeto, três retornos

1. **Portfólio** — fusão de sensores multi-modal é história forte de AI Engineer (poucos makers
   de IoT têm). O motor é **explicável**, não caixa-preta.
2. **Aprendizado** — FastAPI, WebSocket, streams em tempo real, scan de rede, RTSP + visão
   computacional local (YOLO/OpenCV), e o design pattern de fusão sobre uma interface comum.
3. **Uso real** — ferramenta de vigilância de verdade na casa do Augusto, usando o hardware que
   ele já tem (2 câmeras Tapo, rede) + ESP32 quando quiser vitais. **Ligável/desligável sob
   demanda e portável** — roda leve (só rede) sem pesar no PC, ativa câmera/CSI quando quiser
   amplificar, e pode ser levado pra outro lugar sem exigir um host 100% dedicado.

### Restrição inegociável: privacidade

Presença/respiração é biometria; **vídeo do quarto é ainda mais sensível.** Nada da casa real
pode ir para a internet. Isso molda toda a arquitetura (Seção 2) e ganha regras extras para
vídeo (Seção 7).

---

## 2. Arquitetura de dois planos (privacidade por design)

O mesmo **frontend** roda em dois lugares; o **backend + fusão + fontes reais existem só no
Plano A**. Nunca se misturam.

```
┌──────────────────────────────────────────────┐     ┌──────────────────────────────────────┐
│  PLANO A — PRIVADO (casa real)               │     │  PLANO B — VITRINE (público)         │
│                                              │     │                                      │
│  [rede] [wifi csi] [câmera+CV] ─► Fusion ─►  │     │  Simulador multi-modal (no browser)  │
│  RoomState ─► dashboard LOCAL + SQLite       │     │         │                            │
│  (só derivado: presença/confiança, nunca     │     │         ▼                            │
│   frame de vídeo)                            │     │  MESMO frontend, Cloudflare Pages    │
│                                              │     │  apartamento fictício, ZERO dado real│
│  🔒 nada toca a internet; sem port-forward   │     │  🌍 link que recrutador abre         │
└──────────────────────────────────────────────┘     └──────────────────────────────────────┘
              ▲                                              ▲
              └──────── MESMO FRONTEND (deployado ao público sem backend) ───────┘
                    Plano B é backend-less: fisicamente não alcança nenhum servidor
```

**Por que resolve a privacidade de raiz:**

- O Plano B é **backend-less** — o bundle público não contém nenhum código que nomeie o backend
  privado; a fonte de dado é um simulador que roda no próprio navegador. Ele *fisicamente* não
  alcança a casa real.
- O frontend **auto-seleciona o simulador** sempre que o host **não é** `localhost`/`127.0.0.1`
  (fail-safe: host desconhecido nunca vira "ao vivo").
- O backend privado tem **controle server-side** (bind só em loopback + allowlist de Host header
  contra DNS-rebinding) — o check do frontend é defesa-em-profundidade, não o controle.
- Vídeo: **nunca persiste frame cru**; só o derivado (presença/contagem/confiança). Ver Seção 7.

---

## 3. As camadas (ordem de construção)

| Camada | Nome | O que faz | Entrega |
|--------|------|-----------|---------|
| **1** | **Fusão multi-modal** | N fontes (rede + WiFi CSI + câmera CV + simulada) → FusionEngine → RoomState + dashboard com confiança e "por quê" | **Primeiro build** |
| **2** | Controle | Motor de regras sobre o RoomState (`se ocupado no cômodo X → ação`), MQTT/Home Assistant | Iteração |
| **3** | Segurança | Modo away: preset da Camada 2 (`away + presença fundida → alerta`) | Iteração |
| **4** | Inteligência | LLM narra o RoomState + histórico em linguagem natural | Iteração |

*(Fontes BLE e mmWave entram como novas implementações de `SensorSource` a qualquer momento —
a interface já as suporta.)*

### Escopo do primeiro build = fusão completa com CV

Quando o primeiro build estiver pronto, existe:

1. Backend Python local rodando **4 fontes concorrentes** → FusionEngine → RoomState.
2. Dashboard ao vivo mostrando **estado fundido por cômodo** com barra de confiança + o
   detalhamento por modalidade (a explicação).
3. **NetworkSource** real (scan da LAN, $0) e **CameraSource** com **CV local** (YOLO na RTX
   3060) nas câmeras Tapo — com o **toggle de segurança** (Seção 7).
4. **RuViewSource** (WiFi CSI/vitais) integrado; entra dado real quando o container/ESP32 estiver.
5. **SimulatedSource multi-modal** dirigindo o Plano B (Cloudflare Pages).
6. Pontos de extensão prontos pras Camadas 2/3/4 (Hub + `/api/state`).

---

## 4. Stack técnica

### Plano A — Privado (roda no PC do Augusto, RTX 3060)

| Peça | Tecnologia | Por quê |
|------|-----------|---------|
| Backend + API + WS | **Python + FastAPI** | async, WebSocket, padrão de mercado |
| `SensorSource` (interface) | Python Protocol + campo `modality` | o seam: qualquer modalidade emite o mesmo evento canônico |
| Fonte: rede | **NetworkSource** (scan ARP/ping de MACs conhecidos) | presença nível-casa, $0, real |
| Fonte: WiFi CSI | **RuViewSource** (WebSocket `ws://localhost:3000/ws/sensing`) | vitais + presença através de parede |
| Fonte: câmera | **CameraSource** (OpenCV RTSP + **Ultralytics YOLO** na GPU) | detecção de pessoa, maior precisão; só onde há câmera |
| Fusão | **FusionEngine** (pesos transparentes em config) | combina modalidades → RoomState + confiança + explicação |
| Histórico | **SQLite** | local, zero setup; **só derivado** (nunca frame) |
| Dashboard | **HTML/CSS/JS single-file** | terreno conhecido; servido pelo backend (mesma origem) |

### Plano B — Vitrine pública (Cloudflare Pages, backend-less)

| Peça | Tecnologia | Por quê |
|------|-----------|---------|
| Dashboard | **O MESMO HTML** → Cloudflare Pages | link público; idêntico ao privado |
| Fonte de dado | **Simulador multi-modal em JS no browser** | apartamento fictício com várias "modalidades"; $0, impossível vazar |
| IA / histórico (futuro) | Worker/Edge Function + Gemini (`GEMINI_API_KEY_TEMPLATE`); Supabase **deliberadamente adiado** | Camada 4; fora do primeiro build |

---

## 5. Contratos de dados

### Evento canônico (uma leitura de uma modalidade) — campos EXATOS

```json
{
  "room": "quarto",
  "modality": "camera",
  "presence": true,
  "motion": 9.78,
  "breathing_bpm": 14.2,
  "heart_bpm": 68.0,
  "confidence": 0.94,
  "ts": "2026-07-01T16:20:00+00:00"
}
```

- `modality`: `"wifi_csi" | "network" | "camera" | "sim"` (futuras: `"ble"`, `"mmwave"`).
- `motion`: **potência de banda de movimento crua** (não normalizada 0-1) — ~0-30 no CSI; o
  dashboard escala pra barra. Cada modalidade documenta sua escala.
- `confidence`: confiança **da própria modalidade** naquela leitura (0..1).
- `breathing_bpm`/`heart_bpm`: `float|None` (só CSI fornece; outras mandam `null`).
- `ts`: ISO-8601 UTC com offset `+00:00` (formato do `datetime.isoformat()` do Python).

### RoomState (saída fundida — o produto real)

```json
{
  "room": "quarto",
  "occupied": true,
  "confidence": 0.72,
  "vitals": {"breathing_bpm": 14.2, "heart_bpm": 68.0},
  "sources": [
    {"modality": "network", "presence": false, "confidence": 0.5},
    {"modality": "wifi_csi", "presence": true,  "confidence": 0.61},
    {"modality": "camera",   "presence": false, "confidence": 0.0}
  ],
  "explanation": "rede: vazio · wifi: respiração fraca · câmera: off → 72% ocupado",
  "ts": "2026-07-01T16:20:01+00:00"
}
```

### As interfaces (o seam)

- **Backend `SensorSource`** (Protocol): `events() -> AsyncIterator[SensingEvent]`;
  implementações `NetworkSource`, `RuViewSource`, `CameraSource`, `SimulatedSource`. Cada uma
  declara sua `modality`.
- **`FusionEngine`**: recebe eventos canônicos de todas as fontes, mantém a última leitura por
  `(cômodo, modalidade)`, e emite `RoomState` por cômodo a cada atualização.
- **Frontend `DataProvider`** (interface JS, seam *análogo* — não idêntico): `{ start(onEvent),
  history() }` com `WebSocketProvider` (Plano A) e `SimulatorProvider` (Plano B). No Plano A ele
  consome `RoomState`; no Plano B o simulador gera `RoomState` fictício diretamente.

---

## 6. Fluxo de dado ponta-a-ponta (Camada 1)

**Plano A — Privado:**
```
[rede]     NetworkSource  ──┐
[wifi csi] RuViewSource    ──┤  cada fonte emite evento canônico (com modality)
[câmera]   CameraSource    ──┤        │
[toggle=on → RTSP+YOLO]      │        ▼
                             └──► FusionEngine: última leitura por (cômodo,modalidade)
                                        │  recomputa RoomState (occupied, confidence, explanation)
                                        ├─► SQLite (RoomState + eventos derivados; NUNCA frame)
                                        └─► Hub ─► ws://localhost:8000/ws/live ─► dashboard
                                              (barra de confiança + detalhamento por modalidade)
```

**Plano B — Vitrine:**
```
Simulador multi-modal (JS no browser) ──gera RoomState fictício num timer──►
Dashboard (o MESMO HTML) → Cloudflare Pages   (apartamento inventado; sem backend)
```

---

## 7. Câmeras + CV: regras e o toggle de segurança

**Modelos:** C210 (quarto) — RTSP + ONVIF confirmados. TC40 (quintal) — confirmar no app; se for
bateria/solar, pode só empurrar clipes de evento (usar como fonte de evento, não stream contínuo).

**CV:** OpenCV puxa frames do RTSP; **Ultralytics YOLO (nano)** detecta pessoa na **RTX 3060**
(CUDA). Emite evento canônico `modality:"camera"` com presença/contagem/confiança por cômodo.

**Toggle de segurança (controle de primeira classe):**
- **Kill-switch por câmera, server-side.** Off = o `CameraSource` **fecha o RTSP e não lê/processa
  frame nenhum**. Parada dura, não é esconder na UI. Nada entra na memória.
- **Default à prova de falha: câmeras começam DESLIGADAS no boot.** Ligar é ação consciente. O
  quarto nunca é filmado por padrão.
- **Defesa em camadas:** independente do botão de privacidade local da Tapo (corta a lente no
  hardware). Dois kill-switches independentes.
- Estado **persistido**; indicador ON/OFF **visível** no dashboard; controle via API
  (`GET /api/cameras`, `POST /api/cameras/{id}/toggle`).
- *(Extensão documentada, não v1:* política de agenda "noite = só WiFi, câmera off".)*

**Privacidade de vídeo (regras duras):**
- Frames/clipes **nunca** saem da LAN, **nunca** tocam o Plano B/Cloudflare.
- Persiste **só o derivado** (presença/contagem/bbox/confiança/ts) — **nunca o frame cru**.
- Plano B usa uma modalidade "câmera" **simulada**, jamais imagem real.

---

## 7b. Controle de runtime, recursos e portabilidade

O sistema é **ligável/desligável sob demanda** e não exige o host 100% dedicado.

- **On/off global.** Um interruptor liga/desliga TODAS as fontes. Desligado = nenhuma task
  rodando, footprint ~zero. O PC não carrega o sistema quando você não quer.
- **On/off por fonte.** Cada modalidade liga/desliga em runtime, independente. Dá pra rodar
  **só a rede** (leve, um scan periódico) e **ativar a câmera só quando quiser amplificar** — o
  CV pesado (YOLO/GPU) só consome recurso enquanto a câmera está ligada.
- **Gating de recurso.** Fonte desligada = task cancelada; na câmera, fecha o RTSP e para o CV.
  Sem processamento fantasma.
- **Portabilidade.** Com footprint mínimo (só scan de rede), o Wavr roda leve num laptop levado
  pra outro lugar (ou num mini-PC/Raspberry no futuro pra modo sempre-ligado-leve), ativando
  câmeras/CSI só onde e quando fizer sentido.
- **Controle:** `GET /api/system` (estado global + por fonte), `POST /api/system/toggle`,
  `POST /api/sources/{nome}/toggle`. Refletido no dashboard: interruptor geral + toggle por fonte.

A peça que implementa isso é o **SourceManager** (uma task por fonte habilitada) — é onde o
Sub-plano B (rede, CSI) e o C (câmera) plugam suas fontes. Nasce na fundação (Sub-plano A).

---

## 8. Critérios de sucesso do primeiro build (Camada 1)

- [ ] Backend roda ≥2 fontes reais concorrentes (rede + câmera) + simulada, sem travar uma na outra.
- [ ] Cada fonte emite o evento canônico com `modality` correta; campos normalizados.
- [ ] FusionEngine produz `RoomState` por cômodo com `occupied`, `confidence` e `explanation`.
- [ ] CameraSource roda CV local (YOLO/RTX 3060) no C210 e emite presença por pessoa detectada.
- [ ] Toggle de segurança: desligar a câmera fecha o RTSP e zera eventos daquela câmera; câmeras
      começam desligadas no boot; estado persistido e visível.
- [ ] On/off global para e retoma todas as fontes; footprint cai a ~zero quando desligado.
- [ ] On/off por fonte: dá pra rodar só a rede e ativar a câmera sob demanda; câmera desligada
      não roda CV.
- [ ] NetworkSource detecta presença nível-casa por scan da LAN ($0, sem hardware novo).
- [ ] RuViewSource integrado (dado real quando o container/ESP32 estiver; senão, simulada cobre).
- [ ] Dashboard mostra RoomState fundido: confiança + detalhamento por modalidade, em tempo real.
- [ ] Timeline de leituras recentes (últimas N — não "horas").
- [ ] Trocar/ligar/desligar fontes por config, sem tocar dashboard/fusão.
- [ ] O MESMO HTML deploya no Cloudflare Pages com o simulador multi-modal (Plano B).
- [ ] Nenhum dado real (presença OU frame) sai da LAN: frontend auto-simula fora de localhost;
      backend só em loopback + allowlist de Host; vídeo nunca persiste cru.

---

## 9. Fora de escopo (só seams documentados)
Camada 2 (regras/MQTT), Camada 3 (away-mode/alertas), Camada 4 (IA), fontes BLE/mmWave, agenda de
câmera, Supabase (adiado), modelo de pose do RuView (`--load-rvf`), auth do dashboard local.

## 10. Nome
**Wavr** — cunhado (WiFi wave + estilo Tillr/Ownly), verificado sem produto colidente no espaço de
casa inteligente. Clearance formal de marca só se comercializar.

## 11. Notas de correção (revisão do swarm)
- Porta do RuView WS é **3000** (`ws://localhost:3000/ws/sensing`), não 8765 (não publicada).
- `ts` usa offset `+00:00` (não sufixo `Z`).
- `motion` é potência de banda crua, não 0-1.
- Plano B é **backend-less** (frontend deployado ao público sem servidor), não "código deployado
  duas vezes". Seam frontend×backend é **análogo**, não espelho idêntico.
- Simulador existe em Python (Plano A/dev) e em JS (Plano B): reimplementações paralelas, só
  precisam ser *plausíveis*, não idênticas — divergência é aceita e documentada.
