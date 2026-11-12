# -*- coding: utf-8 -*-
"""KELOLA AKSES PEMBELI — gerbang unduhan & tutorial Story Studio (penjual).

Alur distribusi terkunci kode (2026-11-08):
  - Landing page TIDAK lagi memajang link unduh installer / video tutorial.
  - Pembeli yang sudah dibayar diberi KODE AKSES unik oleh penjual (perintah
    `buat` di bawah). Kode dikonversi jadi SHA-256 dan disimpan di manifest
    publik `akses.json` di repo Pages — kode mentah tidak pernah dibuat publik.
  - Pembeli membuka `akses.html?kode=...` → halaman validasi kode (WebCrypto)
    → baru menampilkan tombol unduh installer + 2 video tutorial.
  - `cabut` menghapus hash dari manifest → akses pembeli terputus pada
    kunjungan berikutnya (halaman selalu memvalidasi ulang).

Perintah (jalankan dari root proyek):
  python kelola_akses.py buat --nama "Budi" [--wa 62812xxx] [--catatan "..."] [--tanpa-buka]
  python kelola_akses.py cabut --kode SS-XXXXX-XXXXX
  python kelola_akses.py list
  python kelola_akses.py link --installer URL [--versi 2.0.1]
  python kelola_akses.py link --pemasangan URL | --penggunaan URL
  python kelola_akses.py deploy [--hapus-tutorial]

Butuh GitHub CLI terpasang & sudah `gh auth login` (repo adinandrian26/story-studio-landing).
Fungsi murni (normalisasi, hash, manifest, pesan WA) dipisah agar bisa dites
offline di backend/test_kelola_akses.py.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import re
import secrets
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent

REPO = "adinandrian26/story-studio-landing"
PAGES_BASE = "https://bikinexplainer.online"  # custom domain GitHub Pages (2026-11-08)
AKSES_JSON = "akses.json"
WA_SELF = "6289634589191"  # self-chat penjual (pola WA-LINK-SELF-CHAT.url)

# Alphabet aman tanpa karakter ambigu — konvensi sama dengan licensing/licensing_studio.py
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
KODE_PREFIX = "SS"

RIWAYAT = ROOT / "riwayat_kode_akses.txt"

# (file lokal, nama file di repo Pages) — dipublish `deploy`
# qris.png boleh belum ada (dibuat setelah QR dari user) — cmd_deploy melewatinya.
DEPLOY_PAGES = [
    ("landing.html", "index.html"),
    ("akses.html", "akses.html"),
    ("qris.png", "qris.png"),
]
# File publik yang DIHAPUS dari Pages saat `deploy --hapus-tutorial`
TUTORIAL_FILES_PAGES = [
    "tutorial/tutorial-pemasangan.mp4",
    "tutorial/tutorial-penggunaan.mp4",
]

# File pendukung ACC-via-HP & aktivasi-lisensi-via-HP yang dipublish
# `deploy --tools` (sinkronkan tiap kelola_akses.py / acc_runner.py /
# license_issuer.py / workflow berubah).
TOOLS_PAGES = [
    ("kelola_akses.py", "kelola_akses.py"),
    (".github/akses_tools/acc_runner.py", ".github/akses_tools/acc_runner.py"),
    (".github/workflows/acc-pembeli.yml", ".github/workflows/acc-pembeli.yml"),
    (".github/akses_tools/license_issuer.py", ".github/akses_tools/license_issuer.py"),
    (".github/workflows/aktivasi-lisensi.yml", ".github/workflows/aktivasi-lisensi.yml"),
    (".github/workflows/cek-wa.yml", ".github/workflows/cek-wa.yml"),
    (".github/akses_tools/queue_film.py", ".github/akses_tools/queue_film.py"),
    (".github/workflows/film-dari-hp.yml", ".github/workflows/film-dari-hp.yml"),
    ("CNAME", "CNAME"),
]


class GhError(RuntimeError):
    """Gagal memanggil GitHub (gh CLI tidak ada, tidak login, HTTP error)."""


# ======================================================================
# Bagian MURNI (tanpa jaringan) — inilah yang dites di backend/test_kelola_akses.py
# ======================================================================

def normalize_kode(raw: str) -> str:
    """Sisi pembeli (JS) & penjual (Python) HARUS identik: buang semua
    karakter non-alphanumeric lalu uppercase. 'ss-ab1c d-efg' -> 'SSAB1CDEFG'."""
    return re.sub(r"[^A-Z0-9]", "", str(raw).upper())


def hash_kode(raw: str) -> str:
    """SHA-256 hex dari kode ternormalisasi (utf-8). Inilah satu-satunya bentuk
    yang disimpan di manifest publik."""
    return hashlib.sha256(normalize_kode(raw).encode("utf-8")).hexdigest()


def generate_kode(existing_hashes=()) -> str:
    """Kode acak format SS-XXXXX-XXXXX (10 char dari 32 alphabet aman, ~50 bit).
    Ulangi sampai hash-nya belum ada di manifest (anti tabrakan)."""
    taken = set(existing_hashes)
    while True:
        body = "".join(secrets.choice(ALPHABET) for _ in range(10))
        kode = f"{KODE_PREFIX}-{body[:5]}-{body[5:]}"
        if hash_kode(kode) not in taken:
            return kode


def fresh_manifest() -> dict:
    return {"rev": 1, "links": {}, "grants": []}


def manifest_from_bytes(data: bytes) -> dict:
    """Parse + validasi bentuk manifest (dipakai penjual & test)."""
    m = json.loads(data.decode("utf-8"))
    if not isinstance(m, dict):
        raise ValueError("akses.json bukan objek")
    if not isinstance(m.get("links", {}), dict):
        raise ValueError("akses.json: 'links' harus objek")
    if not isinstance(m.get("grants", []), list):
        raise ValueError("akses.json: 'grants' harus daftar")
    return m


def manifest_to_bytes(m: dict) -> bytes:
    return (json.dumps(m, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _bump_rev(m: dict) -> None:
    m["rev"] = int(m.get("rev", 0)) + 1


def add_grant(m: dict, kode: str, nama: str, catatan: str = "") -> dict:
    """Tambah grant baru; return grant-nya. Menaikkan rev."""
    grant = {
        "h": hash_kode(kode),
        "nama": (nama or "").strip() or "(tanpa nama)",
        "dibuat": datetime.date.today().isoformat(),
        "catatan": (catatan or "").strip(),
    }
    m.setdefault("grants", []).append(grant)
    _bump_rev(m)
    return grant


def revoke_grant(m: dict, kode: str) -> int:
    """Hapus SEMUA grant dengan hash kode tsb. Return jumlah yang terhapus."""
    h = hash_kode(kode)
    grants = m.get("grants", [])
    sisa = [g for g in grants if g.get("h") != h]
    removed = len(grants) - len(sisa)
    if removed:
        m["grants"] = sisa
        _bump_rev(m)
    return removed


def upsert_link(m: dict, key: str, url: str, label: str = "") -> None:
    entry = {"url": (url or "").strip()}
    if label:
        entry["label"] = label
    m.setdefault("links", {})[key] = entry
    _bump_rev(m)


def link_of(m: dict, key: str):
    """Pembaca link manifest — cermin linkOf() di akses.html (paritas test).
    Return entry {'url', 'label'?} atau None bila tidak ada/tak valid."""
    e = (m.get("links", {}) or {}).get(key) if isinstance(m, dict) else None
    if isinstance(e, dict) and isinstance(e.get("url"), str) and e.get("url"):
        return e
    return None


def find_grant(m: dict, hash_hex: str):
    """Cari grant berdasarkan hash — cermin findGrant() di akses.html."""
    grants = m.get("grants", []) if isinstance(m, dict) else []
    for g in grants:
        if isinstance(g, dict) and g.get("h") == hash_hex:
            return g
    return None


def build_buyer_link(kode: str) -> str:
    """Link satu-klik pembeli: halaman akses dengan kode terisi otomatis."""
    return f"{PAGES_BASE}/akses.html?kode={normalize_kode(kode)}"


def build_buyer_message(kode: str) -> str:
    """Pesan WhatsApp siap kirim ke pembeli (kode + link + langkah aktivasi lisensi lewat WA)."""
    link = build_buyer_link(kode)
    return (
        "Terima kasih sudah membeli Story Studio! \U0001F389\n\n"
        f"Kode akses Anda: {kode}\n"
        f"Buka link ini (unduhan + tutorial terbuka otomatis):\n{link}\n\n"
        "Di halaman itu ada:\n"
        "\u2022 Unduh aplikasi (installer .zip + panduan)\n"
        "\u2022 2 video tutorial: pemasangan & cara penggunaan\n\n"
        "Ekstrak SELURUH ZIP, lalu jalankan start_studio.bat.\n"
        "Python 3.12 64-bit dipasang otomatis jika diperlukan; installer resminya sudah ikut.\n"
        "Tunggu pemasangan paket aplikasi selesai (perlu internet). Jangan tutup jendela hitam.\n"
        "Browser terbuka setelah server siap. Jika tidak terbuka, ketik http://127.0.0.1:8765 di Chrome/Edge.\n\n"
        "AKTIVASI LISENSI (1 kunci = 1 PC, lewat WA):\n"
        "1. Setelah aplikasi terpasang: buka Aktivasi_Lisensi.bat di folder aplikasi, "
        "salin Kode Mesin yang muncul.\n"
        "2. Kirim Kode Mesin itu ke WA ini.\n"
        "3. Saya terbitkan lisensi Anda langsung dari HP - token lisensi dibalas ke WA ini.\n"
        "4. Salin token dari WA (paling gampang lewat WhatsApp Desktop/Web di PC), "
        "tempel di kotak lisensi pada halaman aktivasi Story Studio di PC, "
        'lalu klik "Aktifkan Studio".'
    )


def build_wa_url(number: str, message: str) -> str:
    """wa.me dengan pesan prefill — pola yang sama dengan WA-LINK-SELF-CHAT.url."""
    num = re.sub(r"\D", "", str(number or ""))
    return f"https://wa.me/{num}?text={quote(message)}"


def riwayat_line(tanda: str, kode: str, nama: str, catatan: str = "") -> str:
    """Satu baris buku besar lokal (RIWAYAT). Tanda: 'JUAL' / 'CABUT'."""
    stamp = datetime.date.today().isoformat()
    parts = [stamp, tanda, normalize_kode(kode), (nama or "").replace("|", "/")]
    if catatan:
        parts.append(catatan.replace("|", "/"))
    return " | ".join(parts)


def append_riwayat(line: str) -> None:
    with RIWAYAT.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ======================================================================
# Lapisan GitHub (gh CLI) — satu-satunya script proyek yang benar-benar memanggil gh
# ======================================================================

def run_gh(args: list) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["gh"] + args, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
    except FileNotFoundError:
        raise GhError(
            "gh CLI tidak ditemukan. Pasang GitHub CLI lalu jalankan 'gh auth login' sekali."
        )
    except subprocess.TimeoutExpired:
        raise GhError("Perintah gh timeout (jaringan lambat?). Coba lagi.")


def ensure_gh() -> None:
    proc = run_gh(["--version"])
    if proc.returncode != 0:
        raise GhError("gh CLI tidak bisa dijalankan. Periksa instalasi GitHub CLI.")


def gh_get_file(path: str):
    """Ambil file dari repo Pages. Return (bytes, sha) atau None bila 404.
    File >1MB: Contents API mengirim encoding 'none' (isi tak menyertai) —
    isi diambil ulang via Git Blobs API memakai git_url dari respons."""
    proc = run_gh(["api", f"repos/{REPO}/contents/{path}"])
    if proc.returncode != 0:
        if "HTTP 404" in (proc.stderr or ""):
            return None
        raise GhError(f"Gagal mengambil {path}: {(proc.stderr or '').strip()}")
    data = json.loads(proc.stdout)
    sha = data.get("sha")
    enc = data.get("encoding")
    if enc == "base64":
        return base64.b64decode(data["content"]), sha
    if enc == "none":
        git_url = data.get("git_url")
        if not git_url:
            raise GhError(f"{path}: file besar tanpa git_url — tidak bisa dibaca")
        pb = run_gh(["api", git_url])
        if pb.returncode != 0:
            raise GhError(f"Gagal mengambil blob {path}: {(pb.stderr or '').strip()}")
        blob = json.loads(pb.stdout)
        return base64.b64decode(blob["content"]), sha
    raise GhError(f"{path}: encoding tak terduga ({enc})")


def gh_get_sha(path: str):
    """Sha file di repo Pages tanpa mengunduh isinya (untuk hapus/replace).
    Return None bila file tidak ada."""
    proc = run_gh(["api", f"repos/{REPO}/contents/{path}"])
    if proc.returncode != 0:
        if "HTTP 404" in (proc.stderr or ""):
            return None
        raise GhError(f"Gagal mengambil info {path}: {(proc.stderr or '').strip()}")
    return json.loads(proc.stdout).get("sha")


def gh_put_file(path: str, data: bytes, message: str, sha: str | None):
    """Tulis/replace file via Contents API (sha wajib bila file sudah ada).
    Return sha BARU file (dipakai put-berantai, mis. undo di acc_runner)."""
    body = {"message": message, "content": base64.b64encode(data).decode("ascii")}
    if sha:
        body["sha"] = sha
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump(body, f)
        tmp = f.name
    proc = run_gh(["api", "-X", "PUT", f"repos/{REPO}/contents/{path}",
                   "--input", tmp])
    if proc.returncode != 0:
        raise GhError(f"Gagal menulis {path}: {(proc.stderr or '').strip()}")
    try:
        return json.loads(proc.stdout).get("content", {}).get("sha")
    except Exception:
        return None


def gh_delete_file(path: str, sha: str, message: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump({"message": message, "sha": sha}, f)
        tmp = f.name
    proc = run_gh(["api", "-X", "DELETE", f"repos/{REPO}/contents/{path}",
                   "--input", tmp])
    if proc.returncode != 0:
        raise GhError(f"Gagal menghapus {path}: {(proc.stderr or '').strip()}")


def fetch_manifest():
    """Return (manifest, sha) atau (manifest baru, None) bila belum ada."""
    fetched = gh_get_file(AKSES_JSON)
    if fetched is None:
        return fresh_manifest(), None
    return manifest_from_bytes(fetched[0]), fetched[1]


def put_manifest(m: dict, sha, message: str):
    return gh_put_file(AKSES_JSON, manifest_to_bytes(m), message, sha)


# ======================================================================
# Perintah CLI
# ======================================================================

def cmd_buat(args) -> None:
    ensure_gh()
    nama = (args.nama or "").strip()
    if not nama:
        raise GhError("--nama wajib diisi (untuk buku besar & manifest).")
    m, sha = fetch_manifest()
    kode = generate_kode(g.get("h") for g in m.get("grants", []))
    add_grant(m, kode, nama, args.catatan)
    put_manifest(m, sha, f"akses: kode baru untuk {nama}")

    link = build_buyer_link(kode)
    msg = build_buyer_message(kode)
    wa_num = re.sub(r"\D", "", args.wa) if args.wa else WA_SELF
    wa_url = build_wa_url(wa_num, msg)
    append_riwayat(riwayat_line("JUAL", kode, nama, args.catatan))

    print()
    print("=== KODE AKSES DIBUAT ===")
    print(f"Kode        : {kode}")
    print(f"Nama        : {nama}")
    print(f"Link pembeli: {link}")
    print()
    print("--- Pesan WA (siap kirim) ---")
    print(msg)
    print("--- Akhir pesan ---")
    print()
    print(f"URL WA ({'ke pembeli' if args.wa else 'self-chat, tekan kirim'}): {wa_url}")
    if not args.tanpa_buka:
        webbrowser.open(wa_url)
        print("(Browser WA sudah dibuka — tinggal tekan kirim.)")
    print()
    print("Catatan: manifest bisa butuh sampai ~10 menit menyebar di CDN GitHub Pages.")
    print(f"Buku besar: {RIWAYAT.name}")


def cmd_cabut(args) -> None:
    ensure_gh()
    if len(normalize_kode(args.kode)) < 6:
        raise GhError("Kode terlalu pendek — cek lagi format SS-XXXXX-XXXXX.")
    m, sha = fetch_manifest()
    removed = revoke_grant(m, args.kode)
    if not removed:
        print("Kode tidak ditemukan di manifest (mungkin sudah dicabut sebelumnya).")
        return
    put_manifest(m, sha, f"akses: cabut kode {normalize_kode(args.kode)}")
    append_riwayat(riwayat_line("CABUT", args.kode, "", "dicabut penjual"))
    print(f"OK — {removed} grant dicabut (rev {m['rev']}).")
    print("Akses pembeli terputus pada kunjungan berikutnya (maks ~10 menit CDN).")


def cmd_list(_args) -> None:
    ensure_gh()
    fetched = gh_get_file(AKSES_JSON)
    if fetched is None:
        print("Belum ada akses.json di Pages. Buat kode pertama: python kelola_akses.py buat --nama ...")
        return
    m = manifest_from_bytes(fetched[0])
    print(f"Manifest rev {m.get('rev')} — {len(m.get('grants', []))} grant aktif:")
    for i, g in enumerate(m.get("grants", []), 1):
        cat = f" — {g['catatan']}" if g.get("catatan") else ""
        print(f"  {i}. {g.get('nama')} (dibuat {g.get('dibuat')}, hash {str(g.get('h'))[:10]}…){cat}")
    print("Links:")
    for k, v in m.get("links", {}).items():
        print(f"  {k}: {v.get('url')}")
    print("(Kode mentah memang tidak bisa ditampilkan — yang disimpan hanya hash.)")


def cmd_link(args) -> None:
    ensure_gh()
    pasangan = []
    if args.installer:
        label = f"StoryStudio-Setup-{args.versi}.zip" if args.versi else ""
        pasangan.append(("installer", args.installer, label))
    if args.pemasangan:
        pasangan.append(("tutorial_pemasangan", args.pemasangan, "Video Tutorial 1 — Pemasangan"))
    if args.penggunaan:
        pasangan.append(("tutorial_penggunaan", args.penggunaan, "Video Tutorial 2 — Cara Penggunaan"))
    if args.contoh_autopilot:
        pasangan.append(("contoh_autopilot", args.contoh_autopilot, "Contoh Hasil — Mode Autopilot"))
    if args.contoh_sutradara:
        pasangan.append(("contoh_sutradara", args.contoh_sutradara, "Contoh Hasil — Mode Sutradara"))
    if not pasangan:
        raise GhError("Tidak ada link yang diberikan. Pakai --installer / --pemasangan / --penggunaan.")
    m, sha = fetch_manifest()
    for key, url, label in pasangan:
        upsert_link(m, key, url, label)
        print(f"link {key} -> {url}")
    put_manifest(m, sha, "akses: perbarui links")
    print(f"OK — manifest rev {m['rev']} terbit. (CDN Pages ~10 menit.)")


def cmd_deploy(args) -> None:
    ensure_gh()
    for local, remote in DEPLOY_PAGES:
        src = ROOT / local
        if not src.exists():
            print(f"SKIP {local} (belum ada — deploy dilanjutkan)")
            continue
        fetched = gh_get_file(remote)
        sha = fetched[1] if fetched else None
        gh_put_file(remote, src.read_bytes(), f"deploy {remote}", sha)
        print(f"OK  {local} -> {remote}{' (replace)' if sha else ' (baru)'}")
    if args.hapus_tutorial:
        for p in TUTORIAL_FILES_PAGES:
            sha = gh_get_sha(p)
            if sha is None:
                print(f"SKIP {p} (tidak ada di Pages)")
                continue
            gh_delete_file(p, sha, f"hapus {p} (pindah ke release, akses via kode)")
            print(f"HAPUS {p} dari Pages")
    if args.tools:
        for local, remote in TOOLS_PAGES:
            src = ROOT / local
            if not src.exists():
                print(f"SKIP {local} (belum ada)")
                continue
            fetched = gh_get_file(remote)
            sha = fetched[1] if fetched else None
            gh_put_file(remote, src.read_bytes(), f"deploy tools {remote}", sha)
            print(f"OK  {local} -> {remote}{' (replace)' if sha else ' (baru)'}")
    print("Selesai. Cek langsung di browser (hard-refresh bila perlu).")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        prog="kelola_akses.py",
        description="Kelola kode akses pembeli Story Studio (unduhan + tutorial terkunci kode).",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("buat", help="ACC pembeli: buat kode akses + link WA otomatis")
    p.add_argument("--nama", required=True, help="Nama pembeli (buku besar & manifest)")
    p.add_argument("--wa", help="Nomor WA pembeli (628…); kosong = self-chat penjual")
    p.add_argument("--catatan", help="Catatan bebas, mis. 'transfer BCA 8 Nov'")
    p.add_argument("--tanpa-buka", action="store_true", help="Jangan buka browser WA")
    p.set_defaults(fn=cmd_buat)

    p = sub.add_parser("cabut", help="Cabut akses sebuah kode")
    p.add_argument("--kode", required=True)
    p.set_defaults(fn=cmd_cabut)

    p = sub.add_parser("list", help="Lihat grant aktif & links")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("link", help="Perbarui link installer/tutorial di manifest")
    p.add_argument("--installer", help="URL asset installer di release")
    p.add_argument("--pemasangan", help="URL MP4 tutorial pemasangan (release)")
    p.add_argument("--penggunaan", help="URL MP4 tutorial cara pakai (release)")
    p.add_argument("--contoh-autopilot", help="URL MP4 contoh hasil Mode Autopilot (release)")
    p.add_argument("--contoh-sutradara", help="URL MP4 contoh hasil Mode Sutradara (release)")
    p.add_argument("--versi", help="Versi untuk label installer, mis. 2.0.1")
    p.set_defaults(fn=cmd_link)

    p = sub.add_parser("deploy", help="Publish landing.html+akses.html ke Pages")
    p.add_argument("--hapus-tutorial", action="store_true",
                   help="Sekalian hapus tutorial/*.mp4 publik dari Pages")
    p.add_argument("--tools", action="store_true",
                   help="Sekalian publish tools ACC-via-HP (kelola_akses.py, "
                        "acc_runner.py, workflow) + CNAME ke repo Pages")
    p.set_defaults(fn=cmd_deploy)

    args = ap.parse_args()
    try:
        args.fn(args)
    except GhError as e:
        print(f"GAGAL: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
