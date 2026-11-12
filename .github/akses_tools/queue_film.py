# -*- coding: utf-8 -*-
"""Runner GitHub Actions untuk workflow "Film dari HP" (Story Studio).

Alur: validasi input dari HP (judul/tema/jumlah adegan)
      -> tambah item ke antrean gist (queue.json, secret gist)
      -> WA konfirmasi ke penjual via Fonnte (reuse acc_runner.fonnte_send).

Item antrean diproses AUTOMATIS oleh aplikasi Story Studio di PC penjual
(services/mobile_queue.py) begitu Studio menyala: naskah -> pemeran -> gambar
(+ klip bila diminta), dengan laporan tiap tahap ke WA penjual.

KEAMANAN: gist ini hanya menampung judul film & status — tidak ada token,
kode lisensi, atau data pembeli. Log Actions tidak mencetak isi rahasia.

I/O diinjeksi (fetch_queue / put_queue / send) agar alur bisa dites offline
di backend/test_mobile_queue.py tanpa jaringan.
"""
import json
import os
import re
import sys
import time

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_TOOLS_DIR))
sys.path.insert(0, _TOOLS_DIR)
sys.path.insert(0, _REPO_ROOT)
from acc_runner import fonnte_send  # noqa: E402

QUEUE_FILENAME = "queue.json"

# Tema karakter Fitur Film (harus identik dengan backend/film_themes/*.json)
THEMES = ("F-REAL", "F-STICK", "F-3D")
DEFAULT_THEME = "F-REAL"
MIN_SCENES, MAX_SCENES = 3, 12
MAX_JUDUL = 120


def sanitize_judul(raw: str) -> str:
    """Judul: strip, rapikan spasi, maks 120 char, jangan kosong."""
    cleaned = " ".join(str(raw or "").split()).strip()
    return cleaned[:MAX_JUDUL]


def clamp_scene(raw) -> int:
    """Jumlah adegan 3-12 (default 8); input HP sering berupa string."""
    try:
        n = int(float(str(raw).strip() or 8))
    except (TypeError, ValueError):
        return 8
    return max(MIN_SCENES, min(MAX_SCENES, n))


def normalize_theme(raw: str) -> str:
    """Tema karakter: terima alias bebas, tidak dikenal -> F-REAL."""
    t = str(raw or "").strip().upper().replace("-", "").replace("_", "")
    aliases = {
        "FREAL": "F-REAL", "REAL": "F-REAL", "REALISTIS": "F-REAL", "REALISTIC": "F-REAL",
        "FSTICK": "F-STICK", "STICK": "F-STICK", "STICKMAN": "F-STICK",
        "F3D": "F-3D", "3D": "F-3D", "ANIMASI3D": "F-3D",
    }
    return aliases.get(t, DEFAULT_THEME) if t not in THEMES else t


def parse_queue(text: str) -> dict:
    """Parse isi gist -> {"rev": int, "items": [...]}; korup/reset = mulai baru."""
    try:
        data = json.loads(text or "{}")
    except Exception:
        return {"rev": 0, "items": []}
    if not isinstance(data, dict):
        return {"rev": 0, "items": []}
    items = data.get("items")
    if not isinstance(items, list):
        items = []
    try:
        rev = int(data.get("rev") or 0)
    except (TypeError, ValueError):
        rev = 0
    return {"rev": rev, "items": [it for it in items if isinstance(it, dict)]}


def build_item(judul: str, tema: str, scene, aksi: str = "") -> dict:
    """Item antrean baru. aksi '' = buat film baru; 'images'/'clips' = lanjut
    proyek yang sudah ada (butuh project_id)."""
    item = {
        "id": f"mf_{int(time.time())}_{re.sub(r'[^a-z0-9]', '', judul.lower())[:8] or 'film'}",
        "judul": sanitize_judul(judul),
        "tema": normalize_theme(tema),
        "scene": clamp_scene(scene),
        "status": "queued",
        "project_id": "",
        "note": "",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if aksi in ("images", "clips"):
        item["aksi"] = aksi
    return item


def update_item(data: dict, item_id: str, **changes) -> dict:
    """Perbarui satu item antrean (status/note/project_id) + rev+1."""
    items = data.get("items") or []
    for it in items:
        if it.get("id") == item_id:
            it.update(changes)
            it["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            break
    return {"rev": int(data.get("rev") or 0) + 1, "items": items}


def build_confirm_message(judul: str, tema: str, scene: int, rev: int) -> str:
    """WA konfirmasi ke penjual (aman dicetak/log — tanpa data rahasia)."""
    theme_names = {
        "F-REAL": "Realistis", "F-STICK": "Stickman", "F-3D": "Animasi 3D",
    }
    return (
        "*Story Studio — Film dari HP* ✅\n\n"
        f"Judul: {judul}\n"
        f"Tema karakter: {theme_names.get(tema, tema)}\n"
        f"Jumlah adegan: {scene}\n\n"
        "Film sudah masuk antrean. Otomatis dikerjakan begitu Story Studio "
        "menyala di laptop: naskah → pemeran → gambar (laporan tiap tahap "
        "ke WA ini). Buka Story Studio untuk hasil & render MP4."
    )


def jalankan(gist_id, judul, tema, scene, wa_penjual, fonnte_token,
             fetch_queue, put_queue, send) -> int:
    """Alur lengkap workflow (I/O diinjeksi — dites offline)."""
    judul_clean = sanitize_judul(judul)
    if not judul_clean:
        print("ERROR: judul kosong — isi judul film di form workflow.")
        return 1
    tema_clean = normalize_theme(tema)
    scene_clean = clamp_scene(scene)

    data = parse_queue(fetch_queue(gist_id))
    item = build_item(judul_clean, tema_clean, scene_clean)
    items = [it for it in (data.get("items") or []) if isinstance(it, dict)]
    items.append(item)
    items = items[-40:]  # ring; antrean tidak membengkak
    data = {"rev": int(data.get("rev") or 0) + 1, "items": items}
    put_queue(gist_id, json.dumps(data, ensure_ascii=False, indent=2))
    rev_now = int(data.get("rev") or 0)
    print(f"OK: item {item['id']} masuk antrean (rev {rev_now}, "
          f"{len(data['items'])} item). Judul: {item['judul']}")

    ok, _body = send(
        wa_penjual,
        build_confirm_message(item["judul"], item["tema"], item["scene"], rev_now),
    )
    if not ok:
        # Antrean SUDAH tersimpan — WA gagal tidak membatalkan (beda dengan
        # ACC Pembeli: judul film bukan data rahasia yang wajib lewat WA).
        print("WARN: WA konfirmasi gagal terkirim (cek workflow Cek WA).")
    return 0


def main() -> int:
    gist_id = os.environ.get("GIST_ID", "").strip()
    if not gist_id:
        print("ERROR: GIST_ID belum di-set di workflow.")
        return 1
    # Wrapper fetch/put pakai GitHub REST via PAT gist (GITHUB_TOKEN Actions
    # TIDAK punya akses gist = 403; pakai secret MOBILE_QUEUE_TOKEN).
    import urllib.request

    token = os.environ.get("GIST_TOKEN") or os.environ.get("GH_TOKEN") or ""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "story-studio-queue",
    }

    def fetch_queue(gid: str) -> str:
        req = urllib.request.Request(f"https://api.github.com/gists/{gid}", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        return (data.get("files") or {}).get(QUEUE_FILENAME, {}).get("content") or ""

    def put_queue(gid: str, content: str) -> None:
        body = json.dumps({"files": {QUEUE_FILENAME: {"content": content}}}).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.github.com/gists/{gid}", data=body, headers=headers, method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()

    def send(target: str, message: str):
        return fonnte_send(os.environ.get("FONNTE_TOKEN") or "", target, message)

    wa_penjual = os.environ.get("WA_PENJUAL") or "6289634589191"
    return jalankan(
        gist_id,
        os.environ.get("JUDUL") or "",
        os.environ.get("TEMA") or "",
        os.environ.get("SCENE") or "8",
        wa_penjual,
        os.environ.get("FONNTE_TOKEN") or "",
        fetch_queue, put_queue, send,
    )


if __name__ == "__main__":
    raise SystemExit(main())
