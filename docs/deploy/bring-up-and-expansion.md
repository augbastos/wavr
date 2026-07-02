# Wavr — Bring-up seguro + expansão sem dificuldade

**Objetivo:** rodar seguro nas condições de hoje (laptop + RTX 3060) e poder migrar pro appliance dedicado (VLAN) **a qualquer momento sem reescrever nada** — só trocar onde roda.

## O princípio que garante a expansão fácil

Migrar laptop → appliance tem que ser **mudança de config, não de código**. Isso se sustenta em 4 invariantes que já valem hoje e NÃO podem ser quebrados:

1. **Todo especificidade de deploy vive em env/config** — bind address, URLs de câmera, path do DB, path/threshold do modelo, device de GPU, pesos de fusão. NUNCA hardcode. (O `config.py` já lê tudo de `WAVR_*` — manter assim.)
2. **O serviço não assume que roda no laptop** — nada de path absoluto do laptop, índice de GPU fixo, ou "localhost" presumido além do bind configurável.
3. **Os guards de segurança estão no APP** (loopback bind, allowlist de Host, CSRF `X-Wavr-Local`, câmera boot-OFF, kill-switch, storage só-derivado) — viajam com o código pra qualquer box. A segmentação de rede é **aditiva** no appliance.
4. **Fontes e pesos são config-driven** — adicionar 3ª câmera, ou uma fonte nova (BLE/mmWave), é config + uma classe `SensorSource` nova. O seam já existe (`_default_sources`, `SourceManager.register`). O núcleo não muda.

Enquanto esses 4 valerem, expandir é trivial. O resto do plano é: endurecer agora, containerizar (o habilitador), migrar depois.

---

## Fase 0 — Endurecer o deploy no laptop (funciona hoje)

**Praticidade + segurança básica, sem hardware novo.**

1. **`.env` único como a costura de portabilidade.** Todos os `WAVR_*` num `.env` (já git-ignored; teu padrão `C:\IA\.env`). Segredos (creds RTSP das Tapo) SÓ aqui, nunca em código/git. Esse arquivo é o que migra pro appliance intacto.
   ```
   WAVR_CAM_QUARTO_URL=rtsp://user:pass@192.168.x.x:554/stream1
   WAVR_CAM_QUINTAL_URL=rtsp://user:pass@192.168.x.x:554/stream1
   WAVR_NET_MACS=aa:bb:...,cc:dd:...     # device→pessoa
   WAVR_RUVIEW_URL=ws://localhost:3000/ws/sensing
   WAVR_CAM_CONFIDENCE=0.5
   ```
2. **Rodar sem admin.** `arp -a`, `ping`, cv2/RTSP, YOLO — todos rodam como usuário comum. Confirmar que nada pede elevação. Idealmente um usuário Windows dedicado, separado do teu diário.
3. **Câmeras numa rede isolada JÁ.** A maioria dos roteadores tem "rede de convidados" (SSID separado). Põe as Tapo lá e **bloqueia o egress de internet delas** no roteador — Tapo phone-home é o vazamento de privacidade; corta. Primeiro passo de segmentação, com hardware que já tens.
4. **Loopback fica.** Dashboard continua só-loopback; acesso de outro device só via túnel SSH ou, depois, pelo appliance.
5. **Start em 1 passo.** Um serviço Windows (via NSSM rodando `uvicorn`) com auto-start no login, OU um atalho `.ps1` fixado. Praticidade agora sem esperar o Tauri.

**Pré-requisitos de código antes de ligar câmera real** (do review final — fazer nesta fase, cada um seu mini-plano SDD):
- I2: keep-alive na `CameraSource` (erro transitório não mata a câmera; reconnect estilo RuView).
- I3: `SourceManager._run` remover a própria task ao terminar (source morta não pode reportar `active=True` — o indicador ON/OFF é controle de segurança).
- `threading.Lock` no `_model()` (YOLO 2x em first-detect concorrente); validar `cam_interval > 0`.

---

## Fase 1 — Containerizar (O habilitador da expansão)

**Isso é o movimento de maior alavancagem pra "expandir a qualquer momento".** Uma vez que o backend é uma imagem Docker + `.env`, migrar pra QUALQUER box = `pull` + copiar `.env` + `docker run`. Mesmo ambiente no laptop e no Jetson — acabou "funciona só na minha máquina".

- `Dockerfile` no backend: base com Python 3.11, `pip install -e .[camera]`, entrypoint `uvicorn`.
- GPU passthrough via **nvidia-container-toolkit** — funciona no 3060 (via WSL2 no Windows) E nativo no Jetson. O MESMO compose sobe nos dois.
- `docker-compose.yml`: monta o `.env`, o volume do SQLite (só-derivado), `restart: unless-stopped`.
- Custo: Docker+GPU no Windows precisa WSL2 + toolkit (setup de uma vez). No Jetson é nativo. Vale pela portabilidade.

Depois desta fase, laptop e appliance rodam **a mesma imagem** — a diferença é só o `.env` e a rede.

---

## Fase 2 — Appliance dedicado (quando quiser, sem dor)

**Os 3 eixos no máximo. Migração = trocar onde roda, não o quê.**

- **Hardware:** Jetson Orin Nano (GPU embarcada, YOLO nativo, ~$250-500) — resolve o conflito da GPU estar no laptop. Ou mini-PC + GPU. Roda a MESMA imagem da Fase 1.
- **Rede:** VLAN dedicada pras câmeras + o box Wavr; egress firewallado; dashboard só da LAN confiável. Mesmo teu laptop comprometido não alcança as câmeras.
- **Sempre-ligado:** `restart: unless-stopped` → dashboard é um bookmark, zero passo de start. Melhor praticidade possível pra vigilância 24/7.
- **A migração inteira:** flashar o box → instalar docker + nvidia toolkit → copiar `.env` → `docker compose up -d`. Porque tudo é config, **zero linha de código muda.**

---

## Por que isso te dá "expansão sem dificuldade"

| Quero adicionar... | Custo, dado o design | Toca o núcleo? |
|---|---|---|
| 3ª/4ª câmera | 1 linha em `_default_sources` + URL no `.env` | Não |
| Fonte nova (BLE, mmWave) | 1 classe `SensorSource` + registro | Não (seam pronto) |
| Mudar de box (laptop→Jetson→mini-PC) | `pull` + `.env` + `docker run` | Não |
| Segmentar a rede | Config de roteador/VLAN | Não (guards do app já viajam) |
| Ajustar sensibilidade/pesos | `.env` (`WAVR_*`) | Não |
| Dashboard como app nativo | Tauri em volta do MESMO HTML | Não |

Nenhuma expansão exige reescrever o núcleo — porque o núcleo (fusão + fontes) é agnóstico de onde roda e de quantas fontes tem. É exatamente pra isso que a arquitetura de fonte-comum + config-driven foi feita.

## Ordem recomendada
1. **Agora:** Fase 0 (endurecer laptop + os 3 pré-requisitos de código) → seguro e prático hoje.
2. **Em seguida:** Fase 1 (Docker + GPU) → destrava a portabilidade; a partir daqui expandir é trivial.
3. **Quando o orçamento/uso pedir:** Fase 2 (Jetson + VLAN) → segurança e praticidade máximas, migração sem dor.
