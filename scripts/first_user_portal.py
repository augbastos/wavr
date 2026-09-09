"""One double-click entry point for the first-user test.

The first user cannot open a terminal, type an address, or know a port for
tomorrow's test. This is the one process that does everything between
"double-click a shortcut" and "a QR code is on screen":

    1. regenerate `releases.json` from what is on disk RIGHT NOW
       (`gen_releases_manifest`) -- so a rebuild ten minutes ago is
       reflected, never a manifest left over from an earlier run;
    2. warn, in Portuguese, about anything missing or out of date;
    3. find this machine's LAN (Wi-Fi) address (`lan_ip`);
    4. start the static + download server on it
       (`first_user_static_server`);
    5. open the site in the default browser;
    6. print a QR code for that address (and always the plain URL, since a
       QR renderer is an optional nicety, not something to depend on);
    7. stay up, quietly, until Ctrl+C -- and shut the listening socket down
       cleanly rather than leaving an orphaned server behind.

    python scripts/first_user_portal.py
"""
from __future__ import annotations

import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import first_user_static_server as static_server  # noqa: E402
import gen_releases_manifest as manifest_mod  # noqa: E402
import lan_ip  # noqa: E402

DEFAULT_PORT = static_server.DEFAULT_PORT


def _print(message: str = "") -> None:
    print(message, flush=True)


def build_portal_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/"


def report_generation(manifest: dict[str, Any], warnings: list[str]) -> None:
    """Everything the first user needs to see, in Portuguese, before anyone
    connects."""
    for warning in warnings:
        _print(f"ATENCAO -- {warning}")

    missing = [e for e in manifest["products"] if not e["available"]]
    outdated = [e for e in manifest["products"] if e.get("outdated")]

    if not missing and not outdated:
        _print("Todos os artefatos esperados estao presentes e em dia.")
        return

    if missing:
        _print("")
        _print("Artefato(s) que faltam nesta pasta:")
        for entry in missing:
            urgency = ("CRITICO para o teste de hoje"
                       if entry["maturity"] == "recommended"
                       else "nao e usado hoje")
            _print(f"  - {entry['product']} ({entry['downloadName']}): "
                   f"{entry['missingReason']} [{urgency}]")

    if outdated:
        _print("")
        _print("Artefato(s) que parecem desatualizados:")
        for entry in outdated:
            _print(f"  - {entry['product']}: {entry['outdatedReason']}")
    _print("")


def print_qr(url: str) -> None:
    """Best-effort QR in the terminal. Never required: the URL above always works."""
    try:
        import qrcode
    except ImportError:
        _print("(o pacote 'qrcode' nao esta instalado -- use o link acima; "
               "sem ele nao da para desenhar o QR code aqui)")
        return
    try:
        code = qrcode.QRCode(border=2)
        code.add_data(url)
        code.make(fit=True)
        code.print_ascii(invert=True)
    except Exception as exc:  # pragma: no cover - defensive, never blocks the test
        _print(f"(nao consegui desenhar o QR code: {exc}; use o link acima)")


def _resolve_host() -> str:
    host = lan_ip.get_lan_ip()
    if host:
        return host
    _print("ATENCAO -- nao encontrei um endereco de rede local (Wi-Fi) nesta")
    _print("maquina. Os outros aparelhos NAO vao alcancar este computador.")
    _print("Confira se o Wi-Fi esta ligado. Abrindo so neste computador por ora.")
    return "127.0.0.1"


def _start_server(host: str, manifest: dict[str, Any]) -> ThreadingHTTPServer:
    try:
        return static_server.build_server(host, DEFAULT_PORT, manifest=manifest)
    except OSError:
        _print(f"(a porta {DEFAULT_PORT} ja esta em uso -- usando outra)")
        return static_server.build_server(host, 0, manifest=manifest)


def main() -> int:
    _print("Preparando o teste do Wavr...")
    _print()

    manifest, warnings = manifest_mod.write_manifest()
    report_generation(manifest, warnings)

    host = _resolve_host()
    httpd = _start_server(host, manifest)
    port = httpd.server_address[1]
    url = build_portal_url(host, port)

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    _print(f"Endereco na sua rede: {url}")
    _print("Abrindo no navegador deste computador...")
    try:
        webbrowser.open(url)
    except Exception as exc:  # pragma: no cover - defensive, never fatal
        _print(f"(nao consegui abrir o navegador sozinho: {exc})")

    _print()
    _print("Aponte a camera do celular ou do tablet para este QR code:")
    print_qr(url)
    _print()
    _print("Deixe esta janela aberta enquanto os aparelhos baixam o Wavr.")
    _print("Para parar, feche esta janela ou aperte Ctrl+C.")

    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        _print()
        _print("Encerrando o portal do Wavr...")
        httpd.shutdown()
        httpd.server_close()
        _print("Encerrado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
