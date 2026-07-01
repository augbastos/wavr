# Wavr — Documento de Design

**Data:** 2026-07-01
**Autor:** Augusto Bastos
**Status:** Aprovado (brainstorming) → pronto para plano de implementação

---

## 1. O que é

**Wavr** é um sistema de sensoriamento, controle e vigilância residencial construído sobre
**WiFi sensing (CSI)** — detectar presença, movimento e sinais vitais (respiração, batimento)
de um cômodo sem câmera, sem wearable, através das ondas de rádio do WiFi.

A fonte de dado inicial é o **RuView** (servidor open-source em Rust que expõe CSI via REST +
WebSocket), rodando primeiro em **dados simulados** (container Docker) e depois em **hardware
ESP32 real** (~$9-15 por cômodo).

### Objetivo — um projeto, três retornos

1. **Portfólio** — peça de vitrine para recrutadores/clientes que prova skill de AI Engineer
   (real-time streams, arquitetura limpa, LLM sobre dados).
2. **Aprendizado** — dominar FastAPI, WebSocket, streams em tempo real, MQTT, e um design
   pattern de verdade (inversão de dependência via interface de fonte de dado).
3. **Uso real** — quando o ESP32 chegar, o mesmo software passa a sentir a casa de verdade,
   sem reescrever nada.

### Restrição inegociável: privacidade

Dado de presença/respiração é biometria. **A casa real do Augusto nunca pode ficar exposta na
internet.** Ninguém pode captar a respiração dele de fora. Essa restrição molda toda a
arquitetura (ver Seção 2).

---

## 2. Arquitetura de dois planos (privacidade por design)

O princípio central: **o mesmo código roda em dois lugares, com fontes de dado diferentes, que
nunca se misturam.**

```
┌─────────────────────────────────────┐     ┌──────────────────────────────────────┐
│  PLANO A — PRIVADO (casa real)       │     │  PLANO B — VITRINE (público)         │
│                                      │     │                                      │
│  ESP32 / RuView ──► ingest Python    │     │  gerador de dado SIMULADO            │
│         │                            │     │  (apartamento fictício)              │
│         ▼                            │     │         │                            │
│  dashboard LOCAL (localhost/LAN)     │     │         ▼                            │
│  histórico em SQLite no PC           │     │  MESMO dashboard, Cloudflare Pages   │
│                                      │     │  histórico em Supabase (opcional)    │
│  🔒 NUNCA toca a internet            │     │  🌍 link que recrutador abre         │
│  sem port-forward, sem túnel público │     │  ZERO dado real — tudo fictício      │
└─────────────────────────────────────┘     └──────────────────────────────────────┘
              ▲                                              ▲
              └──────────── MESMO CÓDIGO ────────────────────┘
                    só a "fonte de dado" troca (é uma interface)
```

**Por que resolve a privacidade de raiz:**

- O dado real **não tem caminho de saída** — o dashboard privado só escuta na rede local,
  não é exposto, não sobe pra nuvem. Fisicamente impossível captar de fora.
- A vitrine pública **não tem dado real dentro** — roda sobre um apartamento fictício simulado.
- É a **mesma base de código** deployada duas vezes. A fonte de dado é uma interface (adapter);
  trocar entre "sensor real local" e "simulador" é configuração, não reescrita.

Isso vira também o argumento mais forte do portfólio: *"privacidade por arquitetura — o dado
real é incapaz de sair da LAN; o demo público roda em dado sintético."*

---

## 3. As camadas (ordem de construção)

Cada camada é uma iteração curta que já deixa algo funcionando. Nunca fica um sistema "meio
pronto que não roda".

| Camada | Nome | O que faz | Entrega |
|--------|------|-----------|---------|
| **1** | Fundação | Ingest do stream + normalização + dashboard ao vivo + histórico | **Primeiro build** |
| **2** | Controle | Motor de regras: `se [condição] por [duração] → [ação]` (MQTT/Home Assistant) | Iteração |
| **3** | Segurança | Modo away: um preset da Camada 2 (`away + presença → alerta celular`) | Iteração |
| **4** | Inteligência | LLM (Gemini/Claude) narra o stream em linguagem natural | Iteração (herói do portfólio) |

Cada camada contém a anterior. A Camada 4 é o diferencial de "AI Engineer" (vs. hobbyista de
IoT), mas depende da Camada 1 embaixo.

### Escopo do primeiro build entregável = Camada 1 + esqueleto

Quando o primeiro build estiver pronto, existe:

1. Backend Python rodando local, lendo o stream do RuView e gravando histórico.
2. Um dashboard ao vivo mostrando presença + vitais em tempo real.
3. A interface de fonte de dado (`SensorSource` no backend / `DataProvider` no frontend) —
   então o mesmo dashboard roda no Plano A (dado real) e no Plano B (simulado) só trocando config.
4. Os pontos de extensão prontos pras Camadas 2/3/4 — sem implementá-las ainda.

As Camadas 2→4 viram iterações de ~uma sessão cada.

---

## 4. Stack técnica

### Plano A — Privado (roda no PC do Augusto)

| Peça | Tecnologia | Por quê |
|------|-----------|---------|
| Ingest + backend | **Python + FastAPI** | Padrão moderno pra API + WebSocket em Python; async; skill de mercado |
| `SensorSource` (interface) | Classe Python (Protocol) | O seam do sistema: `RuViewSource` (real/Docker) e `SimulatedSource` (fictício) |
| Histórico | **SQLite** | Já vem no Python, zero setup, arquivo local, nunca sai do PC |
| Dashboard | **HTML/CSS/JS single-file** | Terreno conhecido (pitcher-template); sem build step; polível pelo Impeccable |
| IA (Camada 4, local) | Python → Gemini | Key de `C:\IA\.env`; manda só resumo textual, nunca biometria crua |

### Plano B — Vitrine pública (Cloudflare + Supabase)

| Peça | Tecnologia | Por quê |
|------|-----------|---------|
| Dashboard | **O MESMO HTML** → Cloudflare Pages | Deploy estático; link que recrutador abre; idêntico ao privado |
| Fonte de dado | **Simulador em JS no browser** | Apartamento fictício vive no JS; sem backend, $0, impossível vazar |
| Histórico (opcional) | **Supabase** | Demonstra skill de Postgres; seguro (dado falso) |
| IA (Camada 4, pública) | **Cloudflare Worker / Supabase Edge Function** → Gemini | Reusa padrão do `copilot-ask` **e a chave isolada `GEMINI_API_KEY_TEMPLATE`** já configurada |

---

## 5. As interfaces (o seam que faz os dois planos compartilharem código)

### Forma canônica do evento (contrato único)

Todo evento, venha de onde vier, é normalizado para:

```json
{
  "room": "sala",
  "presence": true,
  "motion": 0.34,
  "breathing_bpm": 14.2,
  "heart_bpm": 68,
  "ts": "2026-07-01T16:20:00Z"
}
```

### Backend — `SensorSource` (Python Protocol)

```
SensorSource
  ├── RuViewSource      → conecta no WebSocket do RuView (real ou Docker simulado)
  └── SimulatedSource   → gera eventos fictícios (apartamento inventado)
```

Trocar a fonte = uma linha de config. Ninguém acima da interface sabe qual está ativa.

### Frontend — `DataProvider` (interface JS)

```
DataProvider
  ├── WebSocketProvider  → conecta no FastAPI (Plano A)
  └── SimulatorProvider  → roda no próprio browser (Plano B)
```

O dashboard fala com um `DataProvider` e nunca com o sensor direto. Mesma forma de evento nos
dois. É o espelho, no frontend, do `SensorSource` do backend.

---

## 6. Fluxo de dado ponta-a-ponta (Camada 1)

**Plano A — Privado:**
```
sensor (ESP32/RuView) ──emite CSI──► RuView server (Rust) ──ws://localhost:8765──►
Backend Python (FastAPI): ① RuViewSource lê o WebSocket → ② normaliza → ③ grava SQLite →
④ retransmite via ws://localhost:8000/ws/live ──► Dashboard (browser): renderiza ao vivo
(no load, puxa histórico via REST)
```

**Plano B — Vitrine:**
```
Simulador JS (no browser) ──emite forma canônica num timer──► Dashboard (o MESMO HTML) → Cloudflare Pages
(Camada 4) botão "o que tá rolando?" → Cloudflare Worker → Gemini (GEMINI_API_KEY_TEMPLATE) → resposta em linguagem natural
```

---

## 7. Realidade de hardware

- **Para a capacidade real do RuView, o hardware é obrigatório.** CSI é dado de camada física
  que quase nenhum aparelho de consumo expõe (PC, celular, roteador de operadora não expõem).
- **ESP32** é o único caminho barato e sem gambiarra (~$9-15/cômodo) — API de CSI oficial da
  Espressif. É por isso que o RuView foi construído nele.
- Alternativas (Intel 5300, Atheros, Nexmon em Raspberry Pi) exigem hardware extra + muito mais
  setup. Nada que o Augusto tem hoje faz CSI de verdade.
- **Mas o software inteiro (90% do valor + todo o portfólio) é construído em dado simulado,
  $0, sem hardware.** O ESP32 só entra no dia de sentir a casa real. Um único ESP32 num cômodo
  já prova o conceito.
- Atalho fraco sem comprar nada: presença por MAC/RSSI do roteador — só detecta quem carrega
  aparelho conectado, não vê corpo/respiração/parede. Outra tecnologia, muito mais crua.
  Possível fonte de dado "real" temporária, mas não é o que impressiona.

---

## 8. Critérios de sucesso do primeiro build (Camada 1)

- [ ] Backend Python conecta no WebSocket do RuView (Docker) e recebe eventos.
- [ ] Eventos são normalizados para a forma canônica e gravados em SQLite.
- [ ] Dashboard mostra, em tempo real, presença + movimento + vitais por cômodo.
- [ ] Dashboard mostra uma timeline do histórico recente (últimas horas).
- [ ] Trocar `RuViewSource` → `SimulatedSource` por config faz o mesmo dashboard rodar em dado
      fictício, sem outra mudança.
- [ ] O mesmo dashboard HTML deploya no Cloudflare Pages rodando o `SimulatorProvider` (Plano B).
- [ ] Nenhum dado real tem caminho de saída da LAN (verificado: sem port-forward/túnel).

---

## 9. Fora de escopo (YAGNI — por agora)

- Camadas 2/3/4 (regras, away-mode, IA) — iterações futuras, só os pontos de extensão no build 1.
- Compra de ESP32 / integração de hardware real — quando o Augusto decidir.
- Integração com Home Assistant / MQTT — entra na Camada 2.
- Autenticação/multi-usuário no dashboard privado — é local, single-user.
- Modelo de pose (`--load-rvf` do RuView) — presença + vitais bastam pro build 1.

---

## 10. Nome

**Wavr** — cunhado (WiFi wave + estilo Tillr/Ownly). Verificado sem produto exato colidente no
espaço de casa inteligente. Clearance de marca registrada formal (USPTO/EUIPO) fica para o caso
de comercialização futura; para portfólio, "sem produto óbvio no mesmo espaço" basta.
