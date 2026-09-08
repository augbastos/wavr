"""Regenerate the two checksum files in `_local/first-user-rc/` from the bytes.

## Why this exists

`SHA256SUMS-completo.txt` said the Windows installer hashed to `f6a0bd72…` on an
afternoon when it hashed to `de9cad46…`. It was not wrong when it was written; it
was written once, by hand, and the installer was rebuilt five times after that.
A checksum file that disagrees with its artifact is worse than no checksum file:
the reader who actually verifies gets a mismatch and no way to tell whether they
were handed a tampered file or a stale list.

`gen_releases_manifest.py` already computes the truth from the bytes and warns
when these files disagree with it, which is how the drift was found. That warning
had no fix to point at, because nothing produced these files. This is that
producer. Run it whenever anything in the folder changes; the manifest generator
should then report no mismatches at all.

The headers are kept verbatim — they carry things a hash cannot say (which APK
replaces which, that nothing here is signed, that the Windows track is not
covered by the short list) — and only the lines of hashes are rewritten.
"""
from __future__ import annotations

import hashlib
import io
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
PASTA = Path(__file__).resolve().parents[1] / "_local" / "first-user-rc"

# The short list is the Android track, in the order the person installs them.
# The long list is everything in the folder, so it also covers the two Windows
# installers and the files that tell somebody what to do with them.
CURTA = ["1-wavr-core-the-brain-INSTALL-ON-ONE-PHONE-ONLY.apk",
         "2-wavr-app-for-phone-and-tablet.apk",
         "3-wavr-wall-screen-kiosk-optional.apk"]
NUNCA = {"SHA256SUMS.txt", "SHA256SUMS-completo.txt"}


def somar(caminho: Path) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for pedaco in iter(lambda: f.read(1 << 20), b""):
            h.update(pedaco)
    return h.hexdigest()


def cabecalho(arquivo: Path) -> str:
    """Everything up to the first hash line, kept exactly as written."""
    texto = io.open(arquivo, encoding="utf-8", newline="").read()
    corte = re.search(r"^[0-9a-f]{64}\s", texto, flags=re.M)
    return texto[:corte.start()] if corte else texto


def escrever(arquivo: Path, nomes: list[str]) -> None:
    original = io.open(arquivo, encoding="utf-8", newline="").read()
    crlf = "\r\n" in original
    linhas = [f"{somar(PASTA / n)}  {n}" for n in nomes]
    texto = cabecalho(arquivo).replace("\r\n", "\n").rstrip("\n") + "\n\n" + "\n".join(linhas) + "\n"
    io.open(arquivo, "w", encoding="utf-8", newline="").write(
        texto.replace("\n", "\r\n") if crlf else texto)
    print(f"  {arquivo.name}: {len(linhas)} arquivos")
    for l in linhas:
        print(f"    {l[:16]}…  {l[66:]}")


def main() -> int:
    if not PASTA.is_dir():
        print(f"pasta ausente: {PASTA}")
        return 1

    faltando = [n for n in CURTA if not (PASTA / n).is_file()]
    if faltando:
        # Silently dropping a name would produce a shorter list that still looks
        # complete, which is the failure this whole file is about.
        print("nao posso gerar a lista curta, faltam arquivos:")
        for n in faltando:
            print("   - " + n)
        return 1

    todos = sorted((p.name for p in PASTA.iterdir()
                    if p.is_file() and p.name not in NUNCA),
                   key=str.lower)
    escrever(PASTA / "SHA256SUMS.txt", CURTA)
    escrever(PASTA / "SHA256SUMS-completo.txt", todos)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
