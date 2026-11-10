# -*- coding: utf-8 -*-
"""Runner GitHub Actions untuk TERBITKAN LISENSI pembeli dari HP
(workflow "Terbitkan Lisensi" di repo Pages).

Alur: validasi kode mesin -> bangun payload (urutan kunci PERSIS
licensing/licensing_studio.issue_token) -> tanda tangan Ed25519 dengan kunci
privat dari secret STUDIO_LICENSE_PRIVATE_KEY -> self-check tanda tangan
dengan kunci publik yang di-embed di aplikasi -> kirim token via Fonnte WA
ke pembeli + salinan penjual.

KEAMANAN: kunci privat hanya hidup di GitHub secret; token lisensi TIDAK
pernah dicetak ke log/summary (log Actions repo publik bisa dibaca siapa
pun) — token hanya berpindah lewat WhatsApp. Tidak ada state yang di-commit
ke repo, jadi bila WA gagal cukup jalankan ulang workflow.

I/O diinjeksi (log/send) dan kunci lewat argumen agar seluruh alur bisa
dites offline di backend/test_license_issuer.py tanpa jaringan.
"""
import base64
import datetime as dt
import json
import os
import re
import sys

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _TOOLS_DIR)
from acc_runner import fonnte_send  # noqa: E402  (satu implementasi Fonnte untuk semua workflow)

FONNTE_URL = "https://api.fonnte.com/send"  # tidak dipakai langsung; dokumentasi

# Kunci publik yang di-embed di backend/services/licensing.py (PUBLIC_KEY_B64).
# Self-check: token yang terbit WAJIB terverifikasi kunci ini; kalau tidak,
# kunci privat di secret bukan pasangannya — JANGAN kirim ke pembeli.
# (Sinkronkan bila kunci pernah dirotasi.)
PUBLIC_KEY_B64 = "bPB9DiFLQvi5THZtFfEKAcxJY-I-i7MgZe0-993suPE"

# Format kode mesin dari services.licensing.machine_code (alphabet tanpa I,L,O,0,1)
MACHINE_RE = re.compile(
    r"^STSS-[A-HJ-NP-Z2-9]{5}-[A-HJ-NP-Z2-9]{5}-[A-HJ-NP-Z2-9]{5}-[A-HJ-NP-Z2-9]{5}$")
PLAN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def build_payload(machine, name, plan, seats, days, now=None):
    """Payload lisensi dengan urutan kunci PERSIS licensing_studio.issue_token."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return {
        "v": 1,
        "plan": plan,
        "seats": seats,
        "buyer": name,
        "machine": machine.strip().upper(),
        "issued": now.strftime("%Y-%m-%d"),
        "expires": (now + dt.timedelta(days=days)).strftime("%Y-%m-%d") if days > 0 else None,
    }


def sign_token(private_pem: bytes, payload: dict) -> str:
    """Tanda tangan Ed25519 — hasil byte-identik dengan licensing_studio.issue_token."""
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key(private_pem, password=None)
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    return f"{b64u(payload_bytes)}.{b64u(key.sign(payload_bytes))}"


def verify_token_signature(token: str) -> bool:
    """Self-check: verifikasi token dengan kunci publik aplikasi."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    payload_b64, _, sig_b64 = token.rpartition(".")
    pub = Ed25519PublicKey.from_public_bytes(_b64u_decode(PUBLIC_KEY_B64))
    try:
        pub.verify(_b64u_decode(sig_b64), _b64u_decode(payload_b64))
        return True
    except InvalidSignature:
        return False


def build_license_message(token: str, payload: dict) -> str:
    berlaku = payload["expires"] or "permanen (tanpa batas)"
    return (
        "LISENSI STORY STUDIO \U0001F511\n\n"
        f"Nama: {payload['buyer']}\n"
        f"Plan: {payload['plan']}\n"
        f"Berlaku: {berlaku}\n"
        f"Kode mesin: {payload['machine']}\n\n"
        "CARA AKTIVASI (di PC yang sudah terinstall Story Studio):\n"
        "1. Salin SELURUH teks token di bawah ini (satu baris penuh).\n"
        "2. Buka aplikasi Story Studio di PC - halaman aktivasi akan muncul.\n"
        '3. Tempel token ke kotak lisensi, lalu klik "Aktifkan Studio".\n\n'
        "TOKEN LISENSI:\n"
        f"{token}"
    )


def jalankan(kode_mesin, nama_pembeli, wa_pembeli, plan, masa_hari,
             private_pem, fonnte_token, wa_penjual, log, send) -> int:
    """Inti alur terbitkan lisensi. Return 0 = sukses.
    `log` TIDAK BOLEH menerima token lisensi — periksa di test."""
    machine = (kode_mesin or "").strip().upper()
    if not MACHINE_RE.match(machine):
        log("GAGAL: Format kode mesin tidak sesuai (harusnya STSS-XXXXX-XXXXX-XXXXX-XXXXX, "
            "tanpa huruf I/L/O dan angka 0/1). Salin ulang Kode Mesin yang muncul di "
            "halaman aktivasi PC pembeli.")
        return 1
    if not (nama_pembeli or "").strip():
        log("GAGAL: Nama pembeli wajib diisi.")
        return 1
    plan = (plan or "studio-1pc").strip() or "studio-1pc"
    if not PLAN_RE.match(plan):
        log(f"GAGAL: Plan '{plan}' tidak valid (pakai studio-1pc/studio-pro/studio-agency).")
        return 1
    try:
        days = max(0, int((masa_hari or "0").strip() or "0"))
    except ValueError:
        log(f"GAGAL: Masa berlaku '{masa_hari}' bukan angka (0 = permanen).")
        return 1
    if not (private_pem or b"").strip():
        log("GAGAL: Secret STUDIO_LICENSE_PRIVATE_KEY belum diset "
            "(Settings > Secrets and variables > Actions).")
        return 1
    if not fonnte_token:
        log("GAGAL: Secret FONNTE_TOKEN belum diset "
            "(Settings > Secrets and variables > Actions).")
        return 1
    if not (wa_penjual or "").strip():
        log("GAGAL: WA_PENJUAL belum diset di workflow.")
        return 1

    payload = build_payload(machine=machine, name=nama_pembeli.strip(),
                            plan=plan, seats=1, days=days)
    try:
        token = sign_token(private_pem, payload)
    except Exception as e:
        log(f"GAGAL: Kunci privat tidak bisa dibaca ({type(e).__name__}). "
            "Pastikan secret berisi isi penuh ed25519_private.pem.")
        return 1
    if not verify_token_signature(token):
        log("GAGAL: Kunci privat di secret TIDAK cocok dengan kunci publik aplikasi. "
            "Lisensi akan ditolak PC pembeli — perbaiki secret STUDIO_LICENSE_PRIVATE_KEY "
            "(isi penuh licensing/ed25519_private.pem).")
        return 1

    pesan = build_license_message(token, payload)
    targets = []
    if (wa_pembeli or "").strip():
        targets.append(wa_pembeli.strip())
    targets.append(wa_penjual)

    gagal = []
    for t in targets:
        ok, body = send(fonnte_token, t, pesan)
        if ok:
            log(f"WA terkirim ke {t}.")
        else:
            gagal.append((t, body))

    if gagal:
        for t, body in gagal:
            log(f"GAGAL kirim WA ke {t}: {str(body)[:120]}")
        log("Lisensi TIDAK jadi terkirim (tidak ada state yang berubah). "
            "Periksa perangkat Fonnte (token/sesi WA) lalu jalankan ulang workflow.")
        return 1

    ringkasan = [
        "### Terbitkan Lisensi",
        "",
        f"- Pembeli: {payload['buyer']}",
        f"- Kode mesin: {payload['machine']}",
        f"- Plan: {payload['plan']} | Berlaku: {payload['expires'] or 'permanen (tanpa batas)'}",
        "- Token dikirim via WhatsApp (sengaja tidak ditampilkan di log).",
    ]
    _tulis_summary("\n".join(ringkasan))
    log("SELESAI: lisensi terbit & token terkirim via WA "
        "(isi token sengaja tidak ditampilkan di log demi keamanan).")
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
    pem_text = os.environ.get("STUDIO_LICENSE_PRIVATE_KEY", "")
    return jalankan(
        kode_mesin=os.environ.get("KODE_MESIN", ""),
        nama_pembeli=os.environ.get("NAMA_PEMBELI", ""),
        wa_pembeli=os.environ.get("WA_PEMBELI", ""),
        plan=os.environ.get("PLAN", "studio-1pc"),
        masa_hari=os.environ.get("MASA_HARI", "0"),
        private_pem=pem_text.encode("utf-8") if pem_text else b"",
        fonnte_token=os.environ.get("FONNTE_TOKEN", ""),
        wa_penjual=os.environ.get("WA_PENJUAL", ""),
        log=print,
        send=fonnte_send,
    )


if __name__ == "__main__":
    sys.exit(main())
