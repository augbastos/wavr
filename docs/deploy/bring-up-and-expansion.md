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
   WAVR_NET_MACS=aa:bb:...,cc:dd:...     # device→pessoa
   WAVR_RUVIEW_URL=ws://localhost:3000/ws/sensing
   WAVR_CAM_CONFIDENCE=0.5
   ```
2. **Rodar sem admin.** `arp -a`, `ping`, cv2/RTSP, YOLO — todos rodam como usuário comum. Confirmar que nada pede elevação. Idealmente um usuário Windows dedicado, separado do teu diário.
3. **Câmeras numa rede isolada JÁ.** A maioria dos roteadores tem "rede de convidados" (SSID separado). Põe as Tapo lá e **bloqueia o egress de internet delas** no roteador — Tapo phone-home é o vazamento de privacidade; corta. Primeiro passo de segmentação, com hardware que já tens.
4. **Loopback fica.** Dashboard continua só-loopback; acesso de outro device só via túnel SSH ou, depois, pelo appliance.
5. **Abrir sob demanda — NÃO serviço sempre-ligado (por causa da VRAM, ver abaixo).** No laptop gamer o modelo certo é: abrir Wavr quando quer vigiar (atalho `.ps1` fixado, ou Tauri depois) → processo sobe; fechar → processo morre → VRAM 100% de volta pros jogos. O serviço sempre-ligado é do **appliance** (Fase 2), onde não há jogo competindo por VRAM.

### Ciclo de vida da VRAM (o que tu pediu: usa quando abre, solta quando fecha)

**Só o YOLO (câmera) usa VRAM.** Scan de rede, WiFi CSI, fusão, dashboard, storage = zero GPU. E o design já escopa isso naturalmente:

- **Wavr aberto, câmeras OFF (boot-OFF)** → `_model()` nunca é chamado → **zero VRAM.** Roda presença por rede + CSI enquanto tu joga, sem tocar a GPU.
- **Liga uma câmera** (toggle consciente) → YOLO carrega na primeira detecção → usa VRAM.
- **Fecha o programa** (processo sai) → o driver NVIDIA devolve **100% da VRAM** — garantia limpa e confiável pros jogos. É o "fecho e para de ocupar" que tu quer.

*Nuance:* enquanto o processo vive, o **contexto CUDA** segura umas centenas de MB mesmo com a câmera desligada — porque o modelo fica em cache no processo (`_YOLO_MODEL` global). Duas saídas:
- **Garantia total = fechar o programa** (processo morre → tudo volta). É o caminho recomendado pro laptop.
- **Opcional (deixar Wavr aberto sem segurar VRAM):** ao desligar a ÚLTIMA câmera, descarregar o modelo (`_YOLO_MODEL = None` + `torch.cuda.empty_cache()`) — recupera a maior parte da VRAM sem fechar; o contexto CUDA residual só some no exit. Enhancement pequeno, fazer junto do bring-up de câmera se quiser rodar Wavr o dia todo com câmera on/off sem pesar nos jogos.

**Pré-requisitos de código antes de ligar câmera real** (do review final — fazer nesta fase, cada um seu mini-plano SDD):
- I2: keep-alive na `CameraSource` (erro transitório não mata a câmera; reconnect estilo RuView).
- I3: `SourceManager._run` remover a própria task ao terminar (source morta não pode reportar `active=True` — o indicador ON/OFF é controle de segurança).
- `threading.Lock` no `_model()` (YOLO 2x em first-detect concorrente); validar `cam_interval > 0`.

---

## Fase 1 — Containerizar (O habilitador da expansão)

**Isso é o movimento de maior alavancagem pra "expandir a qualquer momento".** Uma vez que o backend é uma imagem Docker + `.env`, migrar pra QUALQUER box = `pull` + copiar `.env` + `docker run`. Mesmo ambiente no laptop e no Jetson — acabou "funciona só na minha máquina".

**Status:** `backend/Dockerfile` + `docker-compose.yml` (raiz) + `.dockerignore` já existem no repo (base lean, sem torch/cv2 — só network/ruview/sim/fusion/rules/away/narration-503). O variant com câmera/GPU é follow-up (ver abaixo).

### Build + run (appliance/Linux)

```bash
# On the Linux appliance (Jetson/mini-PC):
cp /path/to/.env .env            # your WAVR_* + GEMINI_API_KEY
docker compose up -d --build     # builds the lean base image, starts on 127.0.0.1:8000
# Dashboard from another device on the LAN: SSH tunnel (keeps the loopback guard intact)
ssh -L 8000:127.0.0.1:8000 user@appliance   # then open http://127.0.0.1:8000 locally
```

### Por que `network_mode: host` + bind `127.0.0.1`

O guard de loopback do app (`_LOOPBACK_HOSTS`, ver `wavr/app.py`) confia no peer real da conexão. Com `network_mode: host` (Linux), o processo dentro do container vê a mesma stack de rede do host — bind em `127.0.0.1:8000` preserva o guard **sem tocar em código**. NÃO usar bind `0.0.0.0` numa bridge network: o gateway Docker apareceria como peer não-loopback e o guard rejeitaria (403) todo request, inclusive os legítimos. Acesso de outro device na LAN é via túnel SSH (`ssh -L`), que mantém a conexão local ao container como loopback — mesma postura "loopback-only" já documentada na Fase 0.

### Caveat: Windows (Docker Desktop)

No laptop de desenvolvimento (Windows), `network_mode: host` não funciona como no Linux — o Docker Desktop roda os containers numa VM e o host networking é limitado/não suportado da mesma forma. **No Windows, continuar rodando `uvicorn` direto** (o jeito atual, Fase 0) em vez de Docker. Docker é o caminho appliance/Linux (Fase 1+2); no Windows ele só serve pra testar o build da imagem, não pra rodar o serviço long-lived.

### Variant GPU/câmera (follow-up)

A imagem base é lean (sem torch/cv2) — cobre presença por rede/CSI, mas NÃO detecção por câmera. Para câmera real: build de um variant que instala `pip install -e backend[camera]` sobre uma base `nvidia/cuda` (torch com suporte CUDA), e descomentar o stanza `deploy.resources.reservations.devices` (GPU) no `docker-compose.yml` + instalar `nvidia-container-toolkit` no host (WSL2 no Windows / nativo no Jetson — ver Fase 2). Imagem consideravelmente maior; tratar como entregável separado, não parte do Fase 1 base.

### Segredos

`.env` nunca é copiado pra dentro da imagem — é montado em runtime via `env_file` no compose, e `.dockerignore` exclui `.env` explicitamente do build context (junto de `.venv`, `*.db`, `.git`, `.superpowers`). Nenhuma credencial (RTSP das Tapo, `GEMINI_API_KEY`) chega a ficar em nenhuma layer da imagem.

Depois desta fase, laptop e appliance rodam **a mesma imagem** — a diferença é só o `.env` e a rede.

---

## Radar de posição — hardware

**Expandir monitoramento pra posição (x/y) e postura (posição do corpo) usando hardware minimalista e que já funciona com Python puro.**

- **Tier R0 — radar de 1 cômodo, sem solda (~€15-20):** 1× HLK-LD2450 (~€10-15) + adaptador USB-TTL CP2102/CH340 (~€3-5) + 4 jumpers fêmea-fêmea (5V/GND/TX/RX — atenção: LD2450 usa UART 256000 baud). Liga DIRETO no PC: `WAVR_MMWAVE_PORT=COM3`, `WAVR_MMWAVE_ROOM=sala`, `pip install -e backend[mmwave]`, restart → pontos no radar. Zero ESP32, zero firmware.
- **Tier R1 — cômodo remoto (futuro, +€6-9/cômodo):** LD2450 + ESP32 baratinho; transporte TCP/MQTT é um `frames` generator novo — a classe e o parser NÃO mudam (seam já pronto).
- **Tier R2 — o experimento CSI (RuView, ~€25):** 2× ESP32-S3; quando os frames do RuView tiverem pose/targets, `normalize_ruview` já os aceita (passthrough pronto). Tratar como pesquisa, não como entregável.
- **Tier R3 — postura pelas câmeras que JÁ EXISTEM (€0):** Tapo C210 → `pip install -e backend[camera]` (~5GB, torch CUDA) + ligar `pose=True` no bring-up da câmera → "sentado/em pé/deitado" no radar (sem posição x/y — homografia é follow-up).

**Calibração (documentar honesto):** x/y do LD2450 são no frame DO SENSOR (montado na parede, olhando pro cômodo). V1 assume sensor no canto-origem olhando pro +y; offset/rotação por cômodo = follow-up pequeno quando o hardware chegar.

### Notas de bring-up (do review)

- **(a) Transporte serial do LD2450 — dois issues conhecidos deferidos:** O fluxo serial tem uma race condition na close/read durante shutdown (pior caso: freeze do event loop no Windows) e o buffer não é persistido entre frames — ambos conhecidos no protocolo do componente. O bring-up **DEVE incluir teste de desligar a fonte durante streaming** (não só leitura feliz dos dados normais), pra detectar o freeze antes de escalar pra produção.
- **(b) Decode sign-magnitude:** A convenção de decodificação segue o componente ESPHome `ld2450` (valores x/y/vx/vy). **Confirmar contra o device real** que a interpretação de bits está correta (especialmente quando os valores são negativos ou em boundaries de quarto).

### Privacidade

**Targets (posição x/y) são LIVE-ONLY por decisão:** fluem pelo WebSocket pro dashboard, mas NUNCA são persistidos no SQLite nem publicados no MQTT. Histórico de movimento em disco seria um passivo de privacidade que o Wavr recusa por design — apenas confidência ocupacional (sim/não por cômodo) é armazenada.

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
| 3ª/4ª câmera | adicionar pela seção Câmeras do dashboard, persistido em SQLite | Não |
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
