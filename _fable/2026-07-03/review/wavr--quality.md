# Wavr — Revisão sênior (lente: qualidade/testes)

Repo: `C:/IA/wavr` · data: 2026-07-03 · foco: cobertura de testes, código morto, arquivos gigantes, drift docs/ADR vs código. Auditorias de segurança/perf anteriores já cobriram bastante — aqui olhei regressões e o que audits podem ter perdido.

**Estado geral (bom):** suíte roda verde — `426 passed in 7.33s` (venv 3.14). 40 módulos backend / 44 arquivos de teste, referência de teste para quase todos os módulos. Invariantes de privacidade (vitals/targets nunca em disco, MCP strip de vitals/targets, guard de loopback no `/ws/live`, refusal de domínios sensíveis no `call_ha_service`) têm teste. Os achados abaixo são o que sobra.

---

## P1 — `wavr.db-wal` (19.5 MB) e `wavr.db-shm` commitados num repo PÚBLICO, com timeline de ocupação da casa `[isSecurity]`

**Arquivos:** `wavr.db-wal`, `wavr.db-shm` (raiz do repo) · `.gitignore`

Apesar de o `.gitignore` listar `wavr.db-wal` / `wavr.db-shm` (linhas adicionadas em `d0b4952`), o git **continua rastreando** os dois — `.gitignore` não destrackeia arquivo já commitado. Evidência:

```
$ git ls-files -s wavr.db-shm wavr.db-wal
100644 adce66c… 0  wavr.db-shm
100644 ea0b6ea… 0  wavr.db-wal
$ git cat-file -s $(git rev-parse HEAD:wavr.db-wal)
19578272                     # 19.5 MB no HEAD
$ git cat-file -p HEAD:wavr.db-wal | strings | grep -iE "ocupado|room_states"
room_states … QMsala
 95% ocupado 2026-07-03T00:54:21.960405+00:00
 40% ocupado 2026-07-03T19:26:40.095369+00:00
 …
```

O WAL contém a tabela `room_states` real (occupancy + confidence + timestamps ISO). Não são biométricos (o schema em `storage.py` não tem colunas vitals/targets — ADR-0002 vale), mas **é a timeline de presença da casa do desenvolvedor** (quando cada cômodo esteve ocupado/vazio) publicada em `github.com/augbastos/wavr`. Para um produto "privacy-first" isso é exatamente o sinal "quando a casa fica vazia" que não deveria vazar — além de 19.5 MB de binário volátil poluindo a história do git. O commit `d0b4952` tentou consertar (destrackeou `wavr.db`) mas **esqueceu os companheiros `-wal`/`-shm`**, que é justamente onde os writes recentes não-checkpointed vivem.

**Fix:** `git rm --cached wavr.db-wal wavr.db-shm` (+ os de `backend/` se existirem) e commit; o `.gitignore` já barra o futuro. Considerar reescrever a história (a árvore pública já expôs o blob).

---

## P2 — Servidor MCP marcado como "shipped" mas sem entry point, sem wiring rodado em CI, e o "read-only de verdade" nunca verificado

**Arquivo:** `backend/wavr/mcp.py:334-412` (`build_mcp_server`) · `backend/pyproject.toml:13-21` · `README.md:41-43,73` · `docs/ROADMAP.md`

O README (l.41-43, 73) e o ROADMAP ("Read-only MCP server", "MCP brain on Home Assistant") listam o MCP como **Shipped**. Mas:

1. **Não há como iniciar o servidor.** `pyproject.toml` não tem `[project.scripts]`; não existe `wavr/__main__.py` nem bloco `if __name__ == "__main__"` em `mcp.py` (grep por `__main__|server.run|stdio_server` em `mcp.py` → nada). `make_server_from_app_state`/`build_mcp_server` só têm chamador em testes — grep no código de produção (excluindo `.venv`) não acha nenhum.
2. **O app nunca liga o MCP ao FusionEngine vivo.** `create_app` cria seu próprio `_fusion` e nunca constrói um MCP a partir dele. Mesmo que alguém rode o servidor standalone, ele receberia um `FusionEngine` vazio sem sources — o "brain on HA" descrito não está operacional.
3. **A camada de wiring nunca executa em CI.** CI roda `pip install -e "backend[dev]"` (`.github/workflows`), e `[dev]` **não inclui** o extra `[mcp]`. Logo o corpo de `build_mcp_server` (registro dos `@server.tool()`, l.359-412) nunca roda — `test_mcp.py::test_building_server_without_mcp_sdk_raises_import_error` até confirma que o SDK está ausente e só verifica que `build` levanta `ImportError`. Ou seja: **a garantia "MCP read-only" é por construção mas nunca é asserida** contra o servidor realmente montado (nenhum teste verifica "só 4 read tools + 1 write tool gated são registrados"). Um `@server.tool()` mutante adicionado por engano passaria no CI.

Feature documentada como pronta, mas hoje é inlançável + com o caminho de montagem sem cobertura executada. Ou expõe um launcher (`wavr/mcp/__main__.py` + `[project.scripts]`, ligando ao fusion vivo) e adiciona `mcp` a uma matriz de CI, ou rebaixa a linguagem de "shipped" no README/ROADMAP.

---

## P3 — Metade do `netutils.py` é código morto/não-ligado (speedtest, WoL, ping, PresenceHistory); ROADMAP afirma o contrário

**Arquivo:** `backend/wavr/netutils.py:169-287` · `docs/ROADMAP.md:11`

Só `port_scan_enabled` + `annotate_risks` + `scan_risky_ports` estão ligados em produção (via `netinventory_service.py:27,99`). O resto do módulo não tem nenhum chamador fora de testes:

- `internet_health` + `_default_latency/download/upload` (speedtest, l.101-183) — inclusive documentado como "DELIBERATE EXCEPTION to zero-egress"
- `build_magic_packet` / `send_magic_packet` (Wake-on-LAN, l.186-210)
- `ping_host` (l.229-240)
- `PresenceHistory` (l.243-287)

`api_inventory.py` só expõe `/api/inventory` e `/api/alerts` — nada disso é alcançável. É código testado (`test_netutils.py`) mas sem caminho de produção. O `docs/ROADMAP.md:11` afirma "opt-in port/**speed/WOL** utilities, wired to `/api/inventory` + `/api/alerts`" — **speed e WOL não estão ligados a endpoint nenhum**; só o port-awareness alimenta `risks`. Drift docs↔código. Risco latente: o speedtest carrega a exceção de egress e convida alguém a ligá-lo num produto que se vende como zero-egress. Decidir: expor via API (com gate) ou remover.

---

## P3 — `auth.py::can_view` é morto em produção e a docstring mente sobre onde é usado

**Arquivo:** `backend/wavr/auth.py:77-85`

O comentário na l.77 diz "Role hierarchy helpers used by the per-route gate in app.py." e `can_view` (l.83) promete ser o guard de leitura. Mas `app.py:39` importa `authorize, parse_bearer, can_change_state, in_subnet` — **não** `can_view`. `grep can_view` no código só acha a definição e usos em `test_multidevice*.py`. Ou seja: `can_view` só existe para ser testado; o gate real de leitura no `app.py` é o middleware `loopback_or_authed` (que não chama isto). Função morta em prod + docstring enganosa (sugere que a autorização de GET/`/ws/live` passa por aqui, o que induz erro em quem for auditar o controle de acesso). Remover, ou de fato usar no guard de leitura.

---

## P3 — `frontend/index.html`: app de 1812 linhas / 99 KB num único arquivo, com ZERO teste automatizado

**Arquivo:** `frontend/index.html:1` (99.559 bytes, 1812 linhas)

O cliente inteiro (dashboard, radar, WS `/ws/live`, editor de casa, pairing de companion, PWA) é JS+CSS inline num só HTML. Não existe nenhum teste JS/TS no repo (`find frontend desktop -name "*.test.*" -o -name "*.spec.*"` → vazio). Consequências de qualidade: (a) viola a própria regra do repo "Keep files under 500 lines" por 3.6×; (b) toda a lógica de fronteira do cliente — reconexão de WS, redenção de ticket, `PUT /api/house`, troca sim↔real off-localhost — não tem rede de segurança contra regressão; (c) o mandato do `CLAUDE.md` de rodar `/polish` + `/audit` antes de deploy de UI é manual e não verificável. Backend é exemplar em testabilidade; o frontend é o ponto cego. Mínimo viável: extrair a lógica em módulos e adicionar um smoke test de browser (Playwright já está no ambiente) para o caminho crítico do dashboard.

---

## P3 — Contagem de testes no README desatualizada (386 vs 426 reais)

**Arquivo:** `README.md:56`

"Tests: `python -m pytest backend/tests -q` (386, all hardware mock-tested)." A coleta real é **426** (`pytest --collect-only` → "426 tests collected"; 401 funções `def test_` + 5 parametrizações). Drift pequeno mas é um número factual no README público que já está errado — sinal de que a documentação não acompanha o código. Trocar por 426 (ou remover o número fixo, que envelhece a cada PR).

---

## Notas menores (não pontuadas)

- `serve.py::main` (branch multidevice → TLS + `uvicorn.run`) não tem teste — aceitável para um launcher fino; `ensure_cert` em si está bem coberto em `test_tls.py`.
- A suíte emite `ResourceWarning: unclosed transport` / "I/O operation on closed pipe" em `test_multidevice_wiring`/`test_notifier_wiring` (subprocess/transport não fechado no teardown async no Windows). Não quebra o verde, mas é ruído que pode mascarar um leak real de recurso.
- `app.py` está em 491 linhas — 9 abaixo do teto de 500 do `CLAUDE.md`; qualquer rota nova o estoura. Candidato a fatiar (routers já são o padrão do projeto — ver `api_devices.py`, `api_inventory.py`).
