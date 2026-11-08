# -*- coding: utf-8 -*-
"""Runner GitHub Actions untuk ACC pembeli dari HP (workflow "ACC Pembeli").

Alur: generate kode -> add_grant -> PUT akses.json (gh, auth GITHUB_TOKEN)
      -> kirim pesan WA via Fonnte ke pembeli + salinan ke penjual.

KEAMANAN: kode mentah TIDAK PERNAH dicetak ke log/summary (log Actions repo
publik bisa dibaca siapa pun). Kode hanya berpindah lewat pesan WhatsApp.
Bila pengiriman WA gagal -> grant DIBATALKAN otomatis (tidak ada kode yatim),
runner keluar dengan error generik; penjual jalankan ulang workflow.

I/O diinjeksi (fetch_manifest / put_manifest / send) agar seluruh alur bisa
dites offline di backend/test_acc_runner.py tanpa jaringan.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
# kelola_akses.py dipublish di ROOT repo Pages; cwd checkout = root repo.
_REPO_ROOT = os.path.dirname(os.path.dirname(_TOOLS_DIR))
sys.path.insert(0, _TOOLS_DIR)
sys.path.insert(0, _REPO_ROOT)
import kelola_akses as ka  # noqa: E402

FONNTE_URL = "https://api.fonnte.com/send"


def build_fonnte_payload(target: str, message: str) -> str:
    """Body x-www-form-urlencoded untuk Fonnte (target = nomor digit 628…)."""
    return urllib.parse.urlencode({
        "target": re.sub(r"\D", "", str(target or "")),
        "message": message,
    })


def fonnte_send(token: str, target: str, message: str, timeout: int = 30):
    """Kirim WA via Fonnte. Return (ok, body). body DIBATASI di pemanggil."""
    req = urllib.request.Request(
        FONNTE_URL,
        data=build_fonnte_payload(target, message).encode("utf-8"),
        headers={"Authorization": str(token)},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception as e:  # jaringan/HTTP error -> gagal terkontrol
        return False, f"{type(e).__name__}: {e}"
    try:
        ok = json.loads(body).get("status") in (True, "true")
    except Exception:
        ok = False
    return ok, body


def jalankan(nama, wa_pembeli, catatan, fonnte_token, wa_penjual, log,
             fetch_manifest, put_manifest, send) -> int:
    """Inti alur ACC. Return kode exit (0 = sukses).
    `log` TIDAK BOLEH menerima kode mentah — periksa di test."""
    if not (nama or "").strip():
        log("GAGAL: Nama pembeli wajib diisi.")
        return 1
    if not fonnte_token:
        log("GAGAL: Secret FONNTE_TOKEN belum diset "
            "(Settings > Secrets and variables > Actions).")
        return 1
    if not (wa_penjual or "").strip():
        log("GAGAL: WA_PENJUAL belum diset di workflow.")
        return 1

    m, sha = fetch_manifest()
    kode = ka.generate_kode(g.get("h") for g in m.get("grants", []))
    ka.add_grant(m, kode, nama, catatan)
    sha = put_manifest(m, sha, f"akses: kode baru untuk {nama} (via Actions)")
    pesan = ka.build_buyer_message(kode)

    targets = []
    if (wa_pembeli or "").strip():
        targets.append(wa_pembeli)
    targets.append(wa_penjual)

    gagal = []
    for t in targets:
        ok, body = send(fonnte_token, t, pesan)
        if ok:
            log(f"WA terkirim ke {t}.")
        else:
            gagal.append((t, body))

    if gagal:
        # UNDO lengkap: tidak boleh ada kode yatim di manifest.
        ka.revoke_grant(m, kode)
        put_manifest(m, sha, f"akses: batalkan kode untuk {nama} (WA gagal kirim)")
        for t, body in gagal:
            log(f"GAGAL kirim WA ke {t}: {str(body)[:120]}")
        log("Kode DIBATALKAN otomatis. Periksa perangkat Fonnte (token/sesi WA) "
            "lalu jalankan ulang workflow.")
        return 1

    ringkasan = ["### ACC Pembeli — " + nama.strip(), "",
                 "- Kode + link dikirim via WhatsApp ✅",
                 f"- Area Pembeli: {ka.PAGES_BASE}/akses.html",
                 f"- Total kode aktif: {len(m.get('grants', []))}"]
    if (wa_pembeli or "").strip():
        ringkasan.insert(2, f"- Tujuan pembeli: {wa_pembeli.strip()}")
    _tulis_summary("\n".join(ringkasan))
    log("SELESAI: kode akses terbit & WA terkirim "
        "(isi kode sengaja tidak ditampilkan di log demi keamanan).")
    return 0


def _tulis_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return jalankan(
        nama=os.environ.get("NAMA", ""),
        wa_pembeli=os.environ.get("WA_PEMBELI", ""),
        catatan=os.environ.get("CATATAN", ""),
        fonnte_token=os.environ.get("FONNTE_TOKEN", ""),
        wa_penjual=os.environ.get("WA_PENJUAL", ""),
        log=print,
        fetch_manifest=ka.fetch_manifest,
        put_manifest=ka.put_manifest,
        send=fonnte_send,
    )


if __name__ == "__main__":
    sys.exit(main())
