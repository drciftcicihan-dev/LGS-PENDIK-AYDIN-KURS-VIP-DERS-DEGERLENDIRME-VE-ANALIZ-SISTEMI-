# -*- coding: utf-8 -*-
"""
PENDİK AYDIN KURS MERKEZİ DERS DEĞERLENDİRME VE ANALİZ SİSTEMİ
Python/Flask backend.

Mevcut HTML/CSS/JavaScript arayüzünün görsel yapısını değiştirmeden:
- SQLite ile kullanıcı/sınav/sonuç verilerini kalıcı tutar.
- Soru görsellerini /static/uploads/ altında gerçek dosya olarak saklar.
- /api/data ile mevcut appData yapısını korur.
- /api/results ile öğrencinin sınav sonucunu tek kayıt olarak güvenli biçimde günceller.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import threading

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "system.db"
BACKUP_DIR = BASE_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_KEEP_DAYS = int(os.getenv("BACKUP_KEEP_DAYS", "30"))
BACKUP_LOCK = threading.Lock()
DB_BUSY_TIMEOUT_MS = 15000
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = STATIC_DIR / "uploads"
INDEX_FILE = BASE_DIR / "index.html"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB / request

ALLOWED_IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg", ".tif", ".tiff", ".heic"
}
ALLOWED_IMAGE_MIMES = {
    "image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp",
    "image/svg+xml", "image/tiff", "image/heic", "image/heif"
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=DB_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=15000;")
    conn.execute("PRAGMA synchronous=NORMAL;")  # WAL modu için performans ve güvenliği optimize eder
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def create_database_backup(reason="daily"):
    """SQLite online backup; does not block or modify application data."""
    if not DB_PATH.exists():
        return None
    with BACKUP_LOCK:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = BACKUP_DIR / f"system_{stamp}_{reason}.db"
        src = sqlite3.connect(DB_PATH, timeout=DB_BUSY_TIMEOUT_MS / 1000)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
            check = dst.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise RuntimeError(f"SQLite integrity check failed: {check}")
            dst.commit()
        finally:
            dst.close()
            src.close()
        cutoff = datetime.now().timestamp() - BACKUP_KEEP_DAYS * 86400
        for f in BACKUP_DIR.glob("system_*.db"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
        return str(target)


def start_daily_backup_worker():
    def worker():
        while True:
            try:
                create_database_backup("daily")
            except Exception:
                app.logger.exception("Günlük veritabanı yedeği oluşturulamadı")
            threading.Event().wait(24 * 60 * 60)
    t = threading.Thread(target=worker, name="daily-db-backup", daemon=True)
    t.start()
    return t


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS exams (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS results (
                id TEXT PRIMARY KEY,
                exam_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                data TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(exam_id, user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_results_user
                ON results(user_id);

            CREATE INDEX IF NOT EXISTS idx_results_exam
                ON results(exam_id);
            """
        )

        # İlk kurulumda mevcut HTML sistemindeki örnek yönetici/öğrenci hesapları korunur.
        count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if count == 0:
            seed_users = [
                {
                    "id": 1,
                    "firstName": "CİHAN",
                    "lastName": "YILMAZ",
                    "role": "admin",
                    "grade": "-",
                    "section": "-",
                    "pass": "admin123",
                    "institution": "-",
                },
                {
                    "id": 2,
                    "firstName": "ALİ",
                    "lastName": "DEMİR",
                    "role": "student",
                    "grade": "8",
                    "section": "A",
                    "pass": "123",
                    "institution": "PENDİK",
                },
                {
                    "id": 3,
                    "firstName": "AYŞE",
                    "lastName": "KAYA",
                    "role": "student",
                    "grade": "8",
                    "section": "B",
                    "pass": "123",
                    "institution": "PENDİK",
                },
            ]
            now = utc_now()
            conn.executemany(
                "INSERT INTO users(id, data, updated_at) VALUES (?, ?, ?)",
                [(str(u["id"]), json.dumps(u, ensure_ascii=False), now) for u in seed_users],
            )
        conn.commit()


def _json_load(value: str) -> dict[str, Any]:
    return json.loads(value)


def read_all_data() -> dict[str, list[dict[str, Any]]]:
    with get_db() as conn:
        users = [_json_load(r["data"]) for r in conn.execute(
            "SELECT data FROM users ORDER BY CAST(id AS INTEGER)"
        )]
        exams = [_json_load(r["data"]) for r in conn.execute(
            "SELECT data FROM exams ORDER BY CAST(id AS INTEGER)"
        )]
        results = [_json_load(r["data"]) for r in conn.execute(
            "SELECT data FROM results ORDER BY submitted_at"
        )]
    return {"users": users, "exams": exams, "results": results}


def normalize_payload(payload: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise ValueError("Geçersiz veri gövdesi.")

    normalized: dict[str, list[dict[str, Any]]] = {}
    for key in ("users", "exams", "results"):
        value = payload.get(key, [])
        if isinstance(value, dict):
            value = list(value.values())
        if not isinstance(value, list):
            raise ValueError(f"{key} alanı liste olmalıdır.")
        normalized[key] = [x for x in value if isinstance(x, dict)]
    return normalized


def replace_all_data(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    data = normalize_payload(payload)
    now = utc_now()

    with get_db() as conn:
        # Mevcut uygulamanın appData yapısı korunur; üç koleksiyonun tamamı
        # atomik bir işlemle yenilenir.
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM exams")
        conn.execute("DELETE FROM results")

        for item in data["users"]:
            if item.get("id") is None:
                item["id"] = str(uuid.uuid4())
            conn.execute(
                "INSERT OR REPLACE INTO users(id, data, updated_at) VALUES (?, ?, ?)",
                (str(item["id"]), json.dumps(item, ensure_ascii=False), now),
            )

        for item in data["exams"]:
            if item.get("id") is None:
                item["id"] = str(uuid.uuid4())
            conn.execute(
                "INSERT OR REPLACE INTO exams(id, data, updated_at) VALUES (?, ?, ?)",
                (str(item["id"]), json.dumps(item, ensure_ascii=False), now),
            )

        # Eski sonuçlar examId/userId üzerinden benzersizdir.
        seen: set[tuple[str, str]] = set()
        for item in data["results"]:
            if item.get("id") is None:
                item["id"] = str(uuid.uuid4())
            exam_id = str(item.get("examId", ""))
            user_id = str(item.get("userId", ""))
            if not exam_id or not user_id:
                continue
            key = (exam_id, user_id)
            if key in seen:
                continue
            seen.add(key)
            conn.execute(
                """
                INSERT OR REPLACE INTO results(
                    id, exam_id, user_id, data, submitted_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(item["id"]),
                    exam_id,
                    user_id,
                    json.dumps(item, ensure_ascii=False),
                    str(item.get("submittedAt") or now),
                    now,
                ),
            )

        # Verilerin diske yazılmasını garanti ediyoruz
        conn.commit()

    return read_all_data()


@app.route("/")
def index():
    if not INDEX_FILE.exists():
        return "index.html bulunamadı.", 404
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "service": "Pendik Aydın Ders Değerlendirme ve Analiz Sistemi",
        "database": str(DB_PATH.name),
        "time": utc_now(),
    })


@app.get("/api/data")
def api_get_data():
    return jsonify(read_all_data())


@app.put("/api/data")
def api_put_data():
    try:
        create_database_backup("before_replace")
        payload = request.get_json(silent=False)
        data = replace_all_data(payload)
        return jsonify({"ok": True, "data": data})
    except Exception as exc:
        app.logger.exception("Veri kaydetme hatası")
        return jsonify({"ok": False, "error": f"Veri kaydedilemedi: {exc}"}), 400


@app.post("/api/results")
def api_save_result():
    result = request.get_json(silent=False)
    if not isinstance(result, dict):
        return jsonify({"error": "Geçersiz sonuç verisi."}), 400

    exam_id = str(result.get("examId", ""))
    user_id = str(result.get("userId", ""))
    if not exam_id or not user_id:
        return jsonify({"error": "examId ve userId zorunludur."}), 400

    result["id"] = result.get("id") or int(datetime.now().timestamp() * 1000)
    result["submittedAt"] = result.get("submittedAt") or utc_now()
    now = utc_now()

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO results(
                id, exam_id, user_id, data, submitted_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(exam_id, user_id) DO UPDATE SET
                id=excluded.id,
                data=excluded.data,
                submitted_at=excluded.submitted_at,
                updated_at=excluded.updated_at
            """,
            (
                str(result["id"]),
                exam_id,
                user_id,
                json.dumps(result, ensure_ascii=False),
                str(result["submittedAt"]),
                now,
            ),
        )
        conn.commit()

    return jsonify({"ok": True, "result": result})


@app.post("/api/upload-image")
def api_upload_image():
    if "image" not in request.files:
        return jsonify({"error": "image alanı bulunamadı."}), 400

    file = request.files["image"]
    if not file or not file.filename:
        return jsonify({"error": "Dosya seçilmedi."}), 400

    original_name = secure_filename(file.filename)
    ext = Path(original_name).suffix.lower()
    mimetype = (file.mimetype or "").lower()

    if ext not in ALLOWED_IMAGE_EXTENSIONS and mimetype not in ALLOWED_IMAGE_MIMES:
        return jsonify({"error": "Desteklenmeyen görsel formatı."}), 400

    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        ext = ".jpg"

    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}{ext}"
    target = UPLOAD_DIR / filename
    file.save(target)

    return jsonify({
        "ok": True,
        "filename": filename,
        "url": f"/static/uploads/{filename}",
    })


@app.get("/api/stats")
def api_stats():
    with get_db() as conn:
        users = conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        exams = conn.execute("SELECT COUNT(*) n FROM exams").fetchone()["n"]
        results = conn.execute("SELECT COUNT(*) n FROM results").fetchone()["n"]
    return jsonify({"users": users, "exams": exams, "results": results})


@app.errorhandler(413)
def too_large(_):
    return jsonify({"error": "Yüklenen veri/dosya boyutu sunucu limitini aşıyor."}), 413


if __name__ == "__main__":
    init_db()
    start_daily_backup_worker()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"

    print("=" * 72)
    print("PENDİK AYDIN DERS DEĞERLENDİRME VE ANALİZ SİSTEMİ")
    print(f"Sunucu : http://127.0.0.1:{port}")
    print(f"LAN    : http://<BİLGİSAYAR-IP>:{port}")
    print(f"SQLite : {DB_PATH}")
    print(f"Uploads: {UPLOAD_DIR}")
    print("=" * 72)

    app.run(host=host, port=port, debug=debug, threaded=True)