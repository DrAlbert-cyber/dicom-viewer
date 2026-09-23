# -*- coding: utf-8 -*-
"""
Просмотрщик DICOM-исследований (флюорография / рентгенография).
Своя база данных SQLite + импорт с транспортного диска в .mdb и SQLite.

Возможности:
  • Окна развёрнуты. ◀ / ▶ или ← / → — листание.
  • Карточки исследований пациента слева в просмотре.
  • Сортировка по клику на заголовок столбца.
  • Быстрые фильтры дат: Сегодня, Вчера, За месяц.
  • Печать с предварительным просмотром.
  • Настройки DICOM-сервера (Комета) и медучреждения.
  • Файлы сохраняются в общее хранилище storage/ с именем по пациенту.
"""

import sys
import os
import re
import json
import uuid
import shutil
import socket
import sqlite3
import datetime
import traceback
import warnings
from pathlib import Path

import numpy as np

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QFileDialog, QProgressBar,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QMessageBox, QGroupBox, QGridLayout, QSplitter,
    QTextEdit, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QDateEdit, QInputDialog, QListWidget, QListWidgetItem,
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QGraphicsRectItem, QGraphicsLineItem, QGraphicsTextItem,
    QGraphicsItem, QGraphicsObject, QGraphicsSimpleTextItem,
    QGraphicsEllipseItem, QGraphicsItemGroup,
    QAbstractItemView, QTabWidget, QSizePolicy, QSpinBox, QCheckBox
)
from PyQt5.QtCore import (
    Qt, QThread, pyqtSignal, QSettings, QDate, QRectF, QPointF, QLineF,
    QTimer, QSize
)
from PyQt5.QtGui import (
    QFont, QColor, QBrush, QIcon, QPixmap, QImage, QPen, QPainter,
    QTransform, QFontMetrics, QPageSize, QPageLayout
)

# ---------- Печать ----------
try:
    from PyQt5.QtPrintSupport import (
        QPrinter, QPrintDialog, QPrintPreviewDialog
    )
    HAS_PRINT = True
except ImportError:
    HAS_PRINT = False

# ---------- DICOM ----------
warnings.filterwarnings(
    "ignore",
    message=".*Unknown encoding.*",
    category=UserWarning,
    module="pydicom.charset",
)

try:
    import pydicom
    HAS_PYDICOM = True
except ImportError:
    HAS_PYDICOM = False

# ---------- MDB ----------
try:
    from access_parser import AccessParser
    HAS_ACCESS_PARSER = True
except ImportError:
    HAS_ACCESS_PARSER = False

try:
    from mdb_sync import JackcessWriter
    HAS_JACKCESS = True
except ImportError:
    HAS_JACKCESS = False

# ---------- DICOM networking ----------
try:
    from pynetdicom import AE
    from pynetdicom.sop_class import Verification
    HAS_PYNETDICOM = True
except ImportError:
    HAS_PYNETDICOM = False


# =====================================================================
#  Пути
# =====================================================================
def _app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _resource_dir():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).parent


def _icon_path():
    for p in (_resource_dir() / "icon.ico",
              _app_dir() / "icon.ico"):
        if p.is_file():
            return p
    return None


SETTINGS = QSettings(str(_app_dir() / "viewer.ini"), QSettings.IniFormat)


# =====================================================================
#  Утилиты имён файлов
# =====================================================================
def safe_filename(name):
    """Убирает недопустимые в Windows символы, сохраняет кириллицу."""
    if not name:
        return "UNKNOWN"
    name = str(name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(". ")
    return name[:150] or "UNKNOWN"


def make_dicom_filename(meta, idx=0):
    """
    Формирует имя файла исследования по образцу:
        {LastName}{F}{M}{DD}{MM}{HH}{MM}{SS}.dcm
    Например: "АБРАМОВА +ТА2008095225.dcm"

    idx > 0 — добавляет суффикс _{idx}.
    """
    last = (meta.get("family") or "").strip()
    given = (meta.get("given") or "").strip()
    middle = (meta.get("middle") or "").strip()

    first_initial = given[:1].upper() if given else ""
    middle_initial = middle[:1].upper() if middle else ""

    study_date = meta.get("study_date") or ""
    study_time = meta.get("study_time") or ""

    # DD MM из "YYYY-MM-DD"
    dd = mm = "00"
    if len(study_date) >= 10:
        try:
            parts = study_date.split("-")
            if len(parts) >= 3:
                mm = parts[1].zfill(2)[:2]
                dd = parts[2].zfill(2)[:2]
        except Exception:
            pass

    # HH MM SS из "HH:MM:SS"
    hh, mi, ss = "00", "00", "00"
    if study_time:
        try:
            tparts = study_time.split(":")
            if len(tparts) >= 3:
                hh = tparts[0].zfill(2)[:2]
                mi = tparts[1].zfill(2)[:2]
                ss = tparts[2][:2].zfill(2)
            elif len(tparts) >= 2:
                hh = tparts[0].zfill(2)[:2]
                mi = tparts[1].zfill(2)[:2]
        except Exception:
            pass

    base = f"{last}{first_initial}{middle_initial}{dd}{mm}{hh}{mi}{ss}"
    base = safe_filename(base)
    if not base:
        base = "PATIENT"
    if idx > 0:
        base += f"_{idx}"
    return base + ".dcm"


def find_free_filename(folder, base_name):
    """
    Возвращает свободный путь в папке folder на основе base_name.
    Если base_name занят — пробует _1, _2, _3...
    """
    folder = Path(folder)
    candidate = folder / base_name
    if not candidate.exists():
        return candidate
    stem = Path(base_name).stem
    ext = Path(base_name).suffix or ".dcm"
    k = 1
    while True:
        candidate = folder / f"{stem}_{k}{ext}"
        if not candidate.exists():
            return candidate
        k += 1
        if k > 10000:  # защита от бесконечного цикла
            candidate = folder / f"{stem}_{uuid.uuid4().hex[:8]}{ext}"
            return candidate


# =====================================================================
#  Кодировка
# =====================================================================
def looks_like_misdecoded_cp1251(s):
    if not isinstance(s, str) or not s:
        return False
    if any('\u0400' <= c <= '\u04ff' for c in s):
        return False
    return any('\u0080' <= c <= '\u00ff' for c in s)


def fix_cp1251(s):
    if not isinstance(s, str) or not s:
        return s
    if not looks_like_misdecoded_cp1251(s):
        return s
    for enc_src in ("cp1252", "latin-1"):
        try:
            return s.encode(enc_src).decode("cp1251")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return s


def check_dicom_server(ip, port, our_ae, remote_ae, timeout=3):
    if not ip:
        return False, "Не указан IP-адрес сервера."
    try:
        port = int(port)
        if not (1 <= port <= 65535):
            raise ValueError("Порт должен быть в диапазоне 1…65535.")
    except (TypeError, ValueError) as e:
        return False, f"Некорректный порт: {e}"

    try:
        with socket.create_connection((ip, port), timeout=timeout):
            tcp_ok = True
    except Exception as e:
        return False, (f"Не удалось подключиться к {ip}:{port}\n"
                       f"Причина: {e}")

    if HAS_PYNETDICOM and our_ae and remote_ae:
        try:
            ae = AE(ae_title=our_ae.strip() or "DICOMVIEWER")
            ae.add_requested_context(Verification)
            assoc = ae.associate(ip, port,
                                 ae_title=remote_ae.strip(),
                                 max_pdu=16382)
            if assoc.is_established:
                status = assoc.send_c_echo()
                assoc.release()
                if status and getattr(status, "Status", None) == 0x0000:
                    return True, (f"DICOM C-ECHO успешно.\n"
                                  f"Сервер {remote_ae} @ {ip}:{port} доступен.")
                return False, (f"C-ECHO вернул статус "
                               f"{getattr(status, 'Status', '?')}")
            return False, (f"TCP-порт доступен, но DICOM-ассоциация "
                           f"не установлена.\nПроверьте AE Title «{remote_ae}».")
        except Exception as e:
            return False, (f"TCP-порт доступен, но C-ECHO завершился "
                           f"ошибкой:\n{e}")

    if tcp_ok:
        return True, (f"Порт {ip}:{port} доступен.\n"
                      f"(Установите pynetdicom для полной C-ECHO-проверки.)")
    return False, "Неизвестная ошибка."


# =====================================================================
#  Элемент таблицы с кастомной сортировкой
# =====================================================================
class SortableItem(QTableWidgetItem):
    def __init__(self, text, sort_key=None):
        super().__init__(str(text))
        self._sort_key = sort_key if sort_key is not None else str(text)

    def __lt__(self, other):
        if isinstance(other, SortableItem):
            try:
                return self._sort_key < other._sort_key
            except TypeError:
                return str(self._sort_key) < str(other._sort_key)
        return super().__lt__(other)


# =====================================================================
#  Тёмная тема
# =====================================================================
DARK_QSS = """
QWidget { background-color: #0d1b2a; color: #e0e1dd;
    font-family: 'Segoe UI', 'Inter', sans-serif; font-size: 10pt; }
QMainWindow, QDialog { background-color: #0d1b2a; }
QLabel { background: transparent; }
QLabel#title { font-size: 15pt; font-weight: 700; color: #00d4ff; letter-spacing: 1px; }
QLabel#subtitle { font-size: 9pt; color: #8d99ae; }
QLabel#patientInfo { background-color: #111d2e; border: 1px solid #2d3e50;
    border-radius: 6px; padding: 10px; color: #e0e1dd; }
QLabel#navPosition {
    background-color: #1b263b; border: 1px solid #2d3e50;
    border-radius: 8px; padding: 4px 12px;
    color: #00d4ff; font-weight: 700; font-size: 12pt;
    min-width: 80px; qproperty-alignment: AlignCenter;
}
QLabel#cardTitle { color: #00d4ff; font-weight: 700; font-size: 11pt;
    padding: 4px 4px; }
QLabel#cardSubtitle { color: #8d99ae; font-size: 9pt;
    padding: 0 4px 4px 4px; }
QLineEdit, QComboBox, QDateEdit, QSpinBox, QTextEdit {
    background-color: #1b263b; border: 1px solid #2d3e50;
    border-radius: 6px; padding: 6px 10px; color: #e0e1dd;
    selection-background-color: #00b4d8; }
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus,
QTextEdit:focus { border: 1px solid #00d4ff; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView { background-color: #1b263b; color: #e0e1dd;
    selection-background-color: #00b4d8; }
QPushButton { background-color: #1b263b; border: 1px solid #2d3e50;
    border-radius: 6px; padding: 8px 16px; color: #e0e1dd; font-weight: 500; }
QPushButton:hover { background-color: #22304a; border-color: #00d4ff; }
QPushButton:pressed { background-color: #00b4d8; color: #0d1b2a; }
QPushButton:disabled { background-color: #14213d; color: #4a5568; }
QPushButton#primary { background-color: #00b4d8; color: #0d1b2a;
    font-weight: 700; border: none; }
QPushButton#primary:hover { background-color: #26c6e8; }
QPushButton#danger { background-color: #ef476f; color: #fff;
    font-weight: 700; border: none; }
QPushButton#danger:hover { background-color: #ff5c85; }
QPushButton#success { background-color: #06d6a0; color: #0d1b2a;
    font-weight: 700; border: none; }
QPushButton#success:hover { background-color: #10e0aa; }
QPushButton#nav { text-align: left; padding: 12px 16px; font-size: 11pt; }
QPushButton#nav:checked { background-color: #00b4d8; color: #0d1b2a;
    font-weight: 700; }
QPushButton#quickdate {
    background-color: #1b263b; border: 1px solid #2d3e50;
    border-radius: 6px; padding: 6px 12px; color: #00d4ff;
    font-weight: 600; }
QPushButton#quickdate:hover { background-color: #22304a;
    border-color: #00d4ff; }
QPushButton#quickdate:pressed { background-color: #00b4d8; color: #0d1b2a; }

QPushButton#icon {
    padding: 0; margin: 0;
    min-width: 42px; max-width: 42px;
    min-height: 42px; max-height: 42px;
    font-size: 18pt;
    background-color: #1b263b; border: 1px solid #2d3e50;
    border-radius: 8px; }
QPushButton#icon:hover { background-color: #22304a; border-color: #00d4ff; }
QPushButton#icon:pressed { background-color: #00b4d8; color: #0d1b2a; }
QPushButton#icon:checked { background-color: #00b4d8; color: #0d1b2a;
    border: 2px solid #00d4ff; }
QPushButton#icon:disabled { background-color: #14213d; color: #4a5568; }

QPushButton#icon_primary {
    padding: 0; margin: 0;
    min-width: 42px; max-width: 42px;
    min-height: 42px; max-height: 42px;
    font-size: 18pt;
    background-color: #00b4d8; color: #0d1b2a;
    border: none; border-radius: 8px; font-weight: 700; }
QPushButton#icon_primary:hover { background-color: #26c6e8; }

QPushButton#icon_danger {
    padding: 0; margin: 0;
    min-width: 42px; max-width: 42px;
    min-height: 42px; max-height: 42px;
    font-size: 18pt;
    background-color: #ef476f; color: #fff;
    border: none; border-radius: 8px; font-weight: 700; }
QPushButton#icon_danger:hover { background-color: #ff5c85; }

QPushButton#navarrow {
    padding: 0; margin: 0;
    min-width: 60px; max-width: 60px;
    min-height: 52px; max-height: 52px;
    font-size: 22pt;
    background-color: #00b4d8; color: #0d1b2a;
    border: none; border-radius: 10px; font-weight: 700; }
QPushButton#navarrow:hover { background-color: #26c6e8; }
QPushButton#navarrow:disabled { background-color: #1e3a4c; color: #6b7a8c; }

QListWidget#studyCards {
    background-color: #0a1420; border: 1px solid #2d3e50;
    border-radius: 6px; outline: 0; }
QListWidget#studyCards::item {
    color: #8d99ae; padding: 3px;
    border: 2px solid transparent; border-radius: 8px; margin: 3px; }
QListWidget#studyCards::item:hover {
    background-color: #16263a; border-color: #2d3e50; }
QListWidget#studyCards::item:selected {
    background-color: #00b4d8; color: #0d1b2a; border-color: #00d4ff; }

QGroupBox { border: 1px solid #2d3e50; border-radius: 8px; margin-top: 16px;
    padding-top: 12px; background-color: #111d2e;
    font-weight: 600; color: #00d4ff; }
QGroupBox::title { subcontrol-origin: margin; left: 12px;
    padding: 0 8px; color: #00d4ff; }
QProgressBar { background-color: #1b263b; border: 1px solid #2d3e50;
    border-radius: 9px; text-align: center; color: #e0e1dd;
    height: 24px; font-weight: 600; }
QProgressBar::chunk { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
    stop:0 #0077b6, stop:1 #00d4ff); border-radius: 8px; }
QTableWidget, QListWidget { background-color: #111d2e;
    alternate-background-color: #16263a; gridline-color: #2d3e50;
    border: 1px solid #2d3e50; border-radius: 6px;
    selection-background-color: #00b4d8; selection-color: #0d1b2a; }
QHeaderView::section { background-color: #1b263b; color: #00d4ff;
    padding: 7px 10px; border: none; border-right: 1px solid #2d3e50;
    border-bottom: 1px solid #2d3e50; font-weight: 700; }
QHeaderView::section:hover { background-color: #22304a; }
QTableWidget::item, QListWidget::item { padding: 5px 8px; }
QListWidget::item:selected { background-color: #00b4d8; color: #0d1b2a; }
QScrollBar:vertical { background: #0d1b2a; width: 11px; margin: 0; }
QScrollBar::handle:vertical { background: #2d3e50; border-radius: 5px;
    min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #00d4ff; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #0d1b2a; height: 11px; margin: 0; }
QScrollBar::handle:horizontal { background: #2d3e50; border-radius: 5px;
    min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: #00d4ff; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QStatusBar { background-color: #0a1420; color: #8d99ae; }
QStatusBar::item { border: none; }
QTabWidget::pane { border: 1px solid #2d3e50; border-radius: 6px;
    background-color: #111d2e; }
QTabBar::tab { background-color: #1b263b; color: #8d99ae;
    padding: 10px 22px; border-top-left-radius: 6px;
    border-top-right-radius: 6px; margin-right: 2px; font-weight: 600; }
QTabBar::tab:selected { background-color: #00b4d8; color: #0d1b2a;
    font-weight: 700; }
QSplitter::handle { background-color: #2d3e50; }
QSplitter::handle:horizontal { width: 3px; }
QToolBar { background-color: #111d2e; border: none; spacing: 4px;
    padding: 6px; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #2d3e50; border-radius: 3px; background: #1b263b; }
QCheckBox::indicator:checked { background: #00b4d8; border-color: #00d4ff; }
"""


# =====================================================================
#  SQLite база данных
# =====================================================================
class Database:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def _create_schema(self):
        c = self.conn.cursor()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS patients (
            id INTEGER PRIMARY KEY,
            last_name TEXT, first_name TEXT, middle_name TEXT,
            dob TEXT, sex INTEGER DEFAULT 0,
            phone TEXT, address TEXT, snils TEXT, policy TEXT,
            notes TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS studies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            study_uid TEXT UNIQUE,
            study_date TEXT, study_time TEXT,
            modality TEXT, bodypart TEXT,
            description TEXT, conclusion TEXT,
            file_path TEXT, folder TEXT,
            imported_at TEXT,
            FOREIGN KEY(patient_id) REFERENCES patients(id)
        );
        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            body TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS conclusion_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            body TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        );
        CREATE TABLE IF NOT EXISTS viewer_state (
            study_id INTEGER PRIMARY KEY,
            state_json TEXT,
            FOREIGN KEY(study_id) REFERENCES studies(id)
        );
        CREATE INDEX IF NOT EXISTS idx_studies_patient
            ON studies(patient_id);
        CREATE INDEX IF NOT EXISTS idx_studies_date
            ON studies(study_date);
        CREATE INDEX IF NOT EXISTS idx_studies_uid
            ON studies(study_uid);
        CREATE INDEX IF NOT EXISTS idx_patients_name
            ON patients(last_name, first_name);
        """)
        self.conn.commit()

    def upsert_patient(self, pid, last, first, middle, dob, sex,
                       phone=None, address=None):
        now = datetime.datetime.now().isoformat()
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM patients WHERE id=?", (pid,))
        row = cur.fetchone()
        if row:
            cur.execute("""UPDATE patients SET
                last_name=?, first_name=?, middle_name=?,
                dob=?, sex=?, phone=?, address=?, updated_at=?
                WHERE id=?""",
                (last, first, middle, dob, sex, phone, address, now, pid))
        else:
            cur.execute("""INSERT INTO patients
                (id, last_name, first_name, middle_name, dob, sex,
                 phone, address, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (pid, last, first, middle, dob, sex, phone, address, now))
        self.conn.commit()
        return pid

    def max_patient_id(self):
        r = self.conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM patients").fetchone()
        return r[0]

    def find_patient(self, last, first, dob):
        cur = self.conn.cursor()
        sql = "SELECT id FROM patients WHERE UPPER(TRIM(last_name))=?"
        params = [last.upper().strip()]
        if first:
            sql += " AND UPPER(TRIM(first_name))=?"
            params.append(first.upper().strip())
        if dob:
            sql += " AND dob=?"
            params.append(dob)
        cur.execute(sql, params)
        row = cur.fetchone()
        return row["id"] if row else None

    def get_patient(self, pid):
        return self.conn.execute(
            "SELECT * FROM patients WHERE id=?", (pid,)).fetchone()

    def all_patients(self):
        return self.conn.execute(
            "SELECT * FROM patients ORDER BY last_name, first_name"
        ).fetchall()

    def delete_patient(self, pid):
        self.conn.execute("DELETE FROM studies WHERE patient_id=?", (pid,))
        self.conn.execute("DELETE FROM patients WHERE id=?", (pid,))
        self.conn.commit()

    def add_study(self, patient_id, study_uid, study_date, study_time,
                  modality, bodypart, file_path=None, folder=None):
        now = datetime.datetime.now().isoformat()
        cur = self.conn.cursor()
        cur.execute("""INSERT OR IGNORE INTO studies
            (patient_id, study_uid, study_date, study_time,
             modality, bodypart, file_path, folder, imported_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (patient_id, study_uid, study_date, study_time,
             modality, bodypart, file_path, folder, now))
        self.conn.commit()
        return cur.lastrowid

    def update_study_description(self, study_id, description, conclusion):
        self.conn.execute(
            "UPDATE studies SET description=?, conclusion=? WHERE id=?",
            (description, conclusion, study_id))
        self.conn.commit()

    def get_study(self, sid):
        return self.conn.execute(
            "SELECT * FROM studies WHERE id=?", (sid,)).fetchone()

    def studies_of_patient(self, pid):
        return self.conn.execute("""
            SELECT id, study_uid, study_date, study_time,
                   modality, bodypart, folder, file_path
            FROM studies
            WHERE patient_id=?
            ORDER BY study_date DESC, study_time DESC, id DESC
        """, (pid,)).fetchall()

    def study_exists(self, uid):
        r = self.conn.execute(
            "SELECT 1 FROM studies WHERE study_uid=?", (uid,)).fetchone()
        return r is not None

    def search_studies(self, text="", date_from=None, date_to=None,
                       patient_id=None, only_undescribed=False):
        sql = """
            SELECT s.*, p.last_name, p.first_name, p.middle_name,
                   p.dob AS p_dob, p.sex AS p_sex
            FROM studies s
            JOIN patients p ON p.id = s.patient_id
            WHERE 1=1
        """
        params = []
        if text:
            sql += """ AND (
                UPPER(p.last_name || ' ' || p.first_name || ' ' ||
                      p.middle_name) LIKE ?
                OR s.study_uid LIKE ?
                OR CAST(p.id AS TEXT) LIKE ?
                OR UPPER(s.description) LIKE ?
            )"""
            t = f"%{text.upper()}%"
            params.extend([t, t, t, t])
        if date_from:
            sql += " AND s.study_date >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND s.study_date <= ?"
            params.append(date_to)
        if patient_id is not None:
            sql += " AND s.patient_id = ?"
            params.append(patient_id)
        if only_undescribed:
            sql += " AND (s.description IS NULL OR s.description='')"
        sql += " ORDER BY s.study_date DESC, s.id DESC LIMIT 5000"
        return self.conn.execute(sql, params).fetchall()

    def count_undescribed(self):
        r = self.conn.execute(
            "SELECT COUNT(*) FROM studies WHERE description IS NULL "
            "OR description=''").fetchone()
        return r[0]

    def templates(self):
        return self.conn.execute(
            "SELECT * FROM templates ORDER BY name").fetchall()

    def save_template(self, name, body):
        now = datetime.datetime.now().isoformat()
        self.conn.execute("""INSERT INTO templates(name, body, created_at)
            VALUES (?,?,?)
            ON CONFLICT(name) DO UPDATE SET body=excluded.body""",
            (name, body, now))
        self.conn.commit()

    def delete_template(self, name):
        self.conn.execute("DELETE FROM templates WHERE name=?", (name,))
        self.conn.commit()

    def conclusion_templates(self):
        return self.conn.execute(
            "SELECT * FROM conclusion_templates ORDER BY name").fetchall()

    def save_conclusion_template(self, name, body):
        now = datetime.datetime.now().isoformat()
        self.conn.execute("""INSERT INTO conclusion_templates
            (name, body, created_at) VALUES (?,?,?)
            ON CONFLICT(name) DO UPDATE SET body=excluded.body""",
            (name, body, now))
        self.conn.commit()

    def delete_conclusion_template(self, name):
        self.conn.execute(
            "DELETE FROM conclusion_templates WHERE name=?", (name,))
        self.conn.commit()

    def get_viewer_state(self, study_id):
        r = self.conn.execute(
            "SELECT state_json FROM viewer_state WHERE study_id=?",
            (study_id,)).fetchone()
        return r["state_json"] if r else None

    def set_viewer_state(self, study_id, json_str):
        self.conn.execute("""INSERT INTO viewer_state(study_id, state_json)
            VALUES (?,?)
            ON CONFLICT(study_id) DO UPDATE SET state_json=excluded.state_json""",
            (study_id, json_str))
        self.conn.commit()

    def clear_viewer_state(self, study_id):
        self.conn.execute(
            "DELETE FROM viewer_state WHERE study_id=?", (study_id,))
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


# =====================================================================
#  Общий парсер DICOM-файлов
# =====================================================================
def parse_dicom_meta(ds):
    """Извлекает метаданные из pydicom.Dataset в стандартный словарь."""
    try:
        pn = ds.get("PatientName", "")
        family = str(getattr(pn, "family_name", "") or "")
        given = str(getattr(pn, "given_name", "") or "")
        middle = str(getattr(pn, "middle_name", "") or "")
        if not (family or given):
            parts = str(pn).split("^")
            family = parts[0] if parts else ""
            given = parts[1] if len(parts) > 1 else ""
            middle = parts[2] if len(parts) > 2 else ""
    except Exception:
        family, given, middle = "", "", ""

    family = fix_cp1251(family).strip()
    given = fix_cp1251(given).strip()
    middle = fix_cp1251(middle).strip()

    dob = ""
    dob_raw = str(ds.get("PatientBirthDate", "") or "")
    if len(dob_raw) >= 8:
        try:
            dob = datetime.datetime.strptime(
                dob_raw[:8], "%Y%m%d").strftime("%Y-%m-%d")
        except Exception:
            dob = ""

    sex = 0
    if str(ds.get("PatientSex", "") or "").upper() == "M":
        sex = 1

    study_date = str(ds.get("StudyDate", "") or "")
    study_time = str(ds.get("StudyTime", "") or "").split(".")[0]
    sd = ""
    st = ""
    if len(study_date) >= 8:
        try:
            sd = datetime.datetime.strptime(
                study_date[:8], "%Y%m%d").strftime("%Y-%m-%d")
        except Exception:
            pass
    if len(study_time) >= 6:
        st = f"{study_time[:2]}:{study_time[2:4]}:{study_time[4:6]}"

    return {
        "family": family, "given": given, "middle": middle,
        "dob": dob, "sex": sex,
        "study_date": sd, "study_time": st,
        "modality": str(ds.get("Modality", "") or ""),
        "bodypart": fix_cp1251(
            str(ds.get("BodyPartExamined", "") or "")).strip(),
    }


def scan_dicom_folder(source_dir):
    """Возвращает { StudyInstanceUID: {"files": [...], "meta": {...}} }."""
    studies = {}
    for dirpath, _, filenames in os.walk(source_dir):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                ds = pydicom.dcmread(fp, stop_before_pixels=True, force=True)
                uid = str(getattr(ds, "StudyInstanceUID", "") or "").strip()
                if not uid:
                    continue
                if uid not in studies:
                    studies[uid] = {"files": [], "meta": parse_dicom_meta(ds)}
                studies[uid]["files"].append(fp)
            except Exception:
                continue
    return studies


def copy_study_files(files, meta, local_storage):
    """
    Копирует файлы исследования в local_storage плоско,
    с именем по пациенту. Возвращает (первый_скопированный_файл, кол-во).
    """
    if not local_storage:
        return None, 0
    local_storage = Path(local_storage)
    local_storage.mkdir(parents=True, exist_ok=True)

    first_path = None
    copied = 0
    for i, src in enumerate(files):
        base = make_dicom_filename(meta, idx=i)
        dst = find_free_filename(local_storage, base)
        try:
            shutil.copy2(src, dst)
            copied += 1
            if first_path is None:
                first_path = str(dst)
        except Exception:
            continue
    return first_path, copied


# =====================================================================
#  Импорт с транспортного диска
# =====================================================================
class ImportWorker(QThread):
    progress = pyqtSignal(int, int)
    status = pyqtSignal(str)
    row_added = pyqtSignal(dict)
    finished_stats = pyqtSignal(dict)

    def __init__(self, source_dir, db_path, mdb_path=None, local_storage=None):
        super().__init__()
        self.source_dir = Path(source_dir)
        self.db_path = db_path
        self.mdb_path = mdb_path
        self.local_storage = Path(local_storage) if local_storage else None
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        stats = {"total": 0, "imported": 0, "skipped": 0, "errors": 0,
                 "new_studies": 0, "new_patients": 0, "files_copied": 0,
                 "errors_list": []}
        try:
            self._run(stats)
        except Exception as e:
            stats["errors"] += 1
            stats["errors_list"].append(str(e))
            stats["errors_list"].append(traceback.format_exc())
        finally:
            self.finished_stats.emit(stats)

    def _run(self, stats):
        if not HAS_PYDICOM:
            raise RuntimeError("pydicom не установлен.")
        if not self.source_dir.is_dir():
            raise RuntimeError(f"Папка не найдена: {self.source_dir}")

        db = Database(self.db_path)
        try:
            self.status.emit("Сканирование папки…")
            studies = scan_dicom_folder(self.source_dir)
            stats["total"] = len(studies)
            if not studies:
                self.status.emit("DICOM-файлы не найдены.")
                return

            mdb_writer = None
            if self.mdb_path and os.path.isfile(self.mdb_path) and HAS_JACKCESS:
                try:
                    self.status.emit("Подключение к .mdb…")
                    mdb_writer = JackcessWriter(self.mdb_path)
                except Exception as e:
                    stats["errors_list"].append(
                        f"Не удалось открыть .mdb: {e}")

            total = len(studies)
            patient_rows = []
            study_rows = []

            for idx, (uid, info) in enumerate(studies.items(), 1):
                if self._stop:
                    break
                try:
                    self._process(uid, info, db, mdb_writer, stats,
                                  patient_rows, study_rows, idx, total)
                except Exception as e:
                    stats["errors"] += 1
                    stats["errors_list"].append(f"{uid[:24]}: {e}")
                self.progress.emit(idx, total)

            if mdb_writer and (patient_rows or study_rows):
                self.status.emit("Запись в .mdb…")
                try:
                    if patient_rows:
                        cols_p = [
                            "ID", "Sex", "DOB", "TownID", "StreetID",
                            "Phone", "JobID", "RepAudience", "ActiveWorker",
                            "PersCard", "DomesticGroupID", "RiskGroupID",
                            "DecretGroupID", "JobPhone", "FirstName",
                            "MidleName", "PoliceNum", "Passport", "House",
                            "Flat", "LastName", "s_Lineage", "s_GUID",
                            "s_Generation",
                        ]
                        mdb_writer.append_rows("Patients", cols_p, patient_rows)
                    if study_rows:
                        cols_s = [
                            "id", "study_number", "modality_worklist_id",
                            "patient_id", "start_date", "end_date",
                            "study_type", "status", "study_uid", "bodypart",
                            "visit_id", "comments", "accession_number",
                            "s_Lineage", "s_GUID", "s_Generation",
                        ]
                        mdb_writer.append_rows("studies", cols_s, study_rows)
                except Exception as e:
                    stats["errors_list"].append(f"Ошибка записи .mdb: {e}")

            self.status.emit("Готово.")
        finally:
            db.close()

    def _process(self, uid, info, db, mdb_writer,
                 stats, patient_rows, study_rows, idx, total):
        m = info["meta"]
        pretty = f"{m['family']} {m['given']} {m['middle']}".strip() or "—"

        if db.study_exists(uid):
            stats["skipped"] += 1
            self.row_added.emit({
                "status": "SKIP", "name": pretty,
                "date": m["study_date"], "uid": uid[:24] + "…",
                "note": "уже в базе"})
            return

        pid = db.find_patient(m["family"], m["given"], m["dob"])
        is_new_patient = pid is None
        if is_new_patient:
            pid = db.max_patient_id() + 1
            db.upsert_patient(pid, m["family"], m["given"], m["middle"],
                              m["dob"], m["sex"])
            stats["new_patients"] += 1
            if mdb_writer:
                guid = str(uuid.uuid4())
                dob_dt = None
                if m["dob"]:
                    try:
                        dob_dt = datetime.datetime.strptime(m["dob"],
                                                            "%Y-%m-%d")
                    except Exception:
                        pass
                patient_rows.append([
                    pid, m["sex"], dob_dt, None, None, None,
                    0, 0, 0, 0, 0, 0, 0, None,
                    m["given"], m["middle"], None, None, None, None,
                    m["family"], None, guid, 1,
                ])

        # ★ Копирование файлов — плоско в local_storage, имя по пациенту
        first_path = None
        copied = 0
        if self.local_storage:
            try:
                first_path, copied = copy_study_files(
                    info["files"], m, self.local_storage)
                stats["files_copied"] += copied
            except Exception as e:
                stats["errors_list"].append(f"Копирование: {e}")

        db.add_study(pid, uid, m["study_date"], m["study_time"],
                     m["modality"], m["bodypart"],
                     file_path=first_path,
                     folder=str(self.local_storage) if self.local_storage else None)
        stats["new_studies"] += 1
        stats["imported"] += 1

        if mdb_writer:
            sid = db.conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM studies").fetchone()[0]
            s_num = sid
            guid = str(uuid.uuid4())
            start_dt = None
            if m["study_date"]:
                try:
                    start_dt = datetime.datetime.strptime(
                        m["study_date"] + " " + (m["study_time"] or "00:00:00"),
                        "%Y-%m-%d %H:%M:%S")
                except Exception:
                    try:
                        start_dt = datetime.datetime.strptime(
                            m["study_date"], "%Y-%m-%d")
                    except Exception:
                        pass
            study_rows.append([
                sid, s_num, 0, pid,
                start_dt, start_dt, 17, "ARRIVED",
                uid, m["bodypart"] or "3CHEST_F",
                None, None, None, None, guid, 1,
            ])

        self.row_added.emit({
            "status": "OK" if is_new_patient else "NEW",
            "name": pretty, "date": m["study_date"],
            "uid": uid[:24] + "…",
            "note": (f"новый пациент, файлов: {copied}" if is_new_patient
                     else f"новое исследование, файлов: {copied}"),
        })


# =====================================================================
#  Кастомные QGraphicsItem
# =====================================================================
class ResizableTextItem(QGraphicsObject):
    HANDLE_SIZE = 10
    PADDING = 6

    def __init__(self, text, pos=QPointF(0, 0), rect=None, font_size=None):
        super().__init__()
        self._text = text or " "
        if rect is None:
            rect = QRectF(0, 0, 340, 110)
        self._rect = QRectF(rect)
        self._aspect = (self._rect.width() / self._rect.height()
                        if self._rect.height() > 0 else 3.0)
        self._font_size = font_size if font_size else 40
        self.setPos(pos)
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self._resize_handle = None
        self._start_rect = None
        self._start_pos = None
        self._font_size = self._compute_font_size()

    def boundingRect(self):
        s = self.HANDLE_SIZE + 4
        return self._rect.adjusted(-s, -s, s, s)

    def paint(self, painter, option, widget):
        selected = self.isSelected()
        if selected:
            painter.setPen(QPen(QColor("#00d4ff"), 2, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(self._rect)
        painter.setPen(QColor("#ffffff"))
        f = QFont("Segoe UI", self._font_size)
        f.setBold(True)
        painter.setFont(f)
        painter.drawText(self._rect,
                         Qt.AlignCenter | Qt.TextWordWrap, self._text)
        if selected:
            painter.setBrush(QBrush(QColor("#00d4ff")))
            painter.setPen(QPen(QColor("#0d1b2a"), 1))
            for hr in self._handles():
                painter.drawRect(hr)

    def _handles(self):
        r = self._rect
        s = self.HANDLE_SIZE
        return [
            QRectF(r.left() - s / 2, r.top() - s / 2, s, s),
            QRectF(r.center().x() - s / 2, r.top() - s / 2, s, s),
            QRectF(r.right() - s / 2, r.top() - s / 2, s, s),
            QRectF(r.right() - s / 2, r.center().y() - s / 2, s, s),
            QRectF(r.right() - s / 2, r.bottom() - s / 2, s, s),
            QRectF(r.center().x() - s / 2, r.bottom() - s / 2, s, s),
            QRectF(r.left() - s / 2, r.bottom() - s / 2, s, s),
            QRectF(r.left() - s / 2, r.center().y() - s / 2, s, s),
        ]

    def _hit_handle(self, pos):
        if not self.isSelected():
            return None
        for i, hr in enumerate(self._handles()):
            if hr.contains(pos):
                return i
        return None

    def _compute_font_size(self):
        try:
            w = max(20, int(self._rect.width()) - 2 * self.PADDING)
            h = max(20, int(self._rect.height()) - 2 * self.PADDING)
        except Exception:
            return 40

        def fits(size):
            f = QFont("Segoe UI", size)
            f.setBold(True)
            fm = QFontMetrics(f)
            needed = fm.boundingRect(
                0, 0, w, 100000,
                Qt.TextWordWrap | Qt.AlignCenter, self._text)
            return needed.height() <= h and needed.width() <= w

        lo, hi = 8, 300
        best = 8
        while lo <= hi:
            mid = (lo + hi) // 2
            if fits(mid):
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def hoverMoveEvent(self, event):
        h = self._hit_handle(event.pos())
        cursors = [Qt.SizeFDiagCursor, Qt.SizeVerCursor,
                   Qt.SizeBDiagCursor, Qt.SizeHorCursor,
                   Qt.SizeFDiagCursor, Qt.SizeVerCursor,
                   Qt.SizeBDiagCursor, Qt.SizeHorCursor]
        if h is not None:
            self.setCursor(cursors[h])
        else:
            self.setCursor(Qt.SizeAllCursor)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            h = self._hit_handle(event.pos())
            if h is not None:
                self._resize_handle = h
                self._start_rect = QRectF(self._rect)
                self._start_pos = event.scenePos()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resize_handle is not None:
            delta = event.scenePos() - self._start_pos
            h = self._resize_handle
            dx = delta.x()
            dy = delta.y()
            sr = self._start_rect
            aspect = self._aspect
            min_w = 100
            min_h = 40

            if h in (0, 2, 4, 6):
                if h == 0:
                    new_w = sr.width() - dx
                    new_h = sr.height() - dy
                    anchor = sr.bottomRight()
                    sign_w, sign_h = -1, -1
                elif h == 2:
                    new_w = sr.width() + dx
                    new_h = sr.height() - dy
                    anchor = sr.bottomLeft()
                    sign_w, sign_h = +1, -1
                elif h == 4:
                    new_w = sr.width() + dx
                    new_h = sr.height() + dy
                    anchor = sr.topLeft()
                    sign_w, sign_h = +1, +1
                else:
                    new_w = sr.width() - dx
                    new_h = sr.height() + dy
                    anchor = sr.topRight()
                    sign_w, sign_h = -1, +1

                if abs(dx) > abs(dy):
                    new_h = new_w / aspect
                else:
                    new_w = new_h * aspect
                new_w = max(min_w, new_w)
                new_h = max(min_h, new_w / aspect)
                new_w = new_h * aspect

                if sign_w < 0 and sign_h < 0:
                    new_rect = QRectF(anchor.x() - new_w, anchor.y() - new_h,
                                      new_w, new_h)
                elif sign_w > 0 and sign_h < 0:
                    new_rect = QRectF(anchor.x(), anchor.y() - new_h,
                                      new_w, new_h)
                elif sign_w > 0 and sign_h > 0:
                    new_rect = QRectF(anchor.x(), anchor.y(),
                                      new_w, new_h)
                else:
                    new_rect = QRectF(anchor.x() - new_w, anchor.y(),
                                      new_w, new_h)
            elif h == 1:
                new_h = sr.height() - dy
                new_h = max(min_h, new_h)
                new_w = new_h * aspect
                cx = sr.center().x()
                new_rect = QRectF(cx - new_w / 2, sr.bottom() - new_h,
                                  new_w, new_h)
            elif h == 5:
                new_h = sr.height() + dy
                new_h = max(min_h, new_h)
                new_w = new_h * aspect
                cx = sr.center().x()
                new_rect = QRectF(cx - new_w / 2, sr.top(), new_w, new_h)
            elif h == 3:
                new_w = sr.width() + dx
                new_w = max(min_w, new_w)
                new_h = new_w / aspect
                new_h = max(min_h, new_h)
                new_w = new_h * aspect
                cy = sr.center().y()
                new_rect = QRectF(sr.left(), cy - new_h / 2, new_w, new_h)
            elif h == 7:
                new_w = sr.width() - dx
                new_w = max(min_w, new_w)
                new_h = new_w / aspect
                new_h = max(min_h, new_h)
                new_w = new_h * aspect
                cy = sr.center().y()
                new_rect = QRectF(sr.right() - new_w, cy - new_h / 2,
                                  new_w, new_h)
            else:
                new_rect = sr

            self.prepareGeometryChange()
            self._rect = new_rect
            self._font_size = self._compute_font_size()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._resize_handle is not None:
            self._resize_handle = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def to_dict(self):
        return {
            "type": "text", "text": self._text,
            "x": self.pos().x(), "y": self.pos().y(),
            "w": self._rect.width(), "h": self._rect.height(),
            "font_size": self._font_size,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("text", ""),
                   QPointF(d.get("x", 0), d.get("y", 0)),
                   QRectF(0, 0, d.get("w", 340), d.get("h", 110)),
                   d.get("font_size"))


class RulerItem(QGraphicsObject):
    HANDLE_R = 9

    def __init__(self, p1=QPointF(0, 0), p2=QPointF(100, 0),
                 label="100 px", font_size=40):
        super().__init__()
        self._p1 = QPointF(p1)
        self._p2 = QPointF(p2)
        self._label = label
        self._font_size = font_size
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self._resize_handle = None
        self._start_p1 = None
        self._start_p2 = None
        self._start_pos = None

    def boundingRect(self):
        m = 80
        xs = [self._p1.x(), self._p2.x()]
        ys = [self._p1.y(), self._p2.y()]
        return QRectF(min(xs) - m, min(ys) - m,
                      max(xs) - min(xs) + 2 * m,
                      max(ys) - min(ys) + 2 * m)

    def paint(self, painter, option, widget):
        selected = self.isSelected()
        painter.setPen(QPen(QColor("#ff4444"), 4))
        painter.drawLine(self._p1, self._p2)

        mid = QPointF((self._p1.x() + self._p2.x()) / 2,
                      (self._p1.y() + self._p2.y()) / 2)
        f = QFont("Segoe UI", self._font_size)
        f.setBold(True)
        painter.setFont(f)
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(self._label)
        th = fm.height()
        tx = mid.x() - tw / 2
        ty = mid.y() - th - 18

        if selected:
            bg_rect = QRectF(tx - 12, ty - 8, tw + 24, th + 16)
            painter.setBrush(QBrush(QColor(0, 0, 0, 235)))
            painter.setPen(QPen(QColor("#00d4ff"), 2, Qt.DashLine))
            painter.drawRect(bg_rect)
        else:
            bg_rect = QRectF(tx - 10, ty - 6, tw + 20, th + 12)
            painter.setBrush(QBrush(QColor(0, 0, 0, 200)))
            painter.setPen(Qt.NoPen)
            painter.drawRect(bg_rect)

        painter.setPen(QColor("#ffffff"))
        painter.drawText(QPointF(tx, ty + fm.ascent()), self._label)

        if selected:
            painter.setBrush(QBrush(QColor("#00d4ff")))
            painter.setPen(QPen(QColor("#0d1b2a"), 2))
            painter.drawEllipse(self._p1, self.HANDLE_R, self.HANDLE_R)
            painter.drawEllipse(self._p2, self.HANDLE_R, self.HANDLE_R)

    def _hit_handle(self, pos):
        if not self.isSelected():
            return None
        if (pos - self._p1).manhattanLength() < 3 * self.HANDLE_R:
            return "p1"
        if (pos - self._p2).manhattanLength() < 3 * self.HANDLE_R:
            return "p2"
        return None

    def hoverMoveEvent(self, event):
        h = self._hit_handle(event.pos())
        if h:
            self.setCursor(Qt.CrossCursor)
        else:
            self.setCursor(Qt.SizeAllCursor)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            h = self._hit_handle(event.pos())
            if h:
                self._resize_handle = h
                self._start_p1 = QPointF(self._p1)
                self._start_p2 = QPointF(self._p2)
                self._start_pos = event.scenePos()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resize_handle is not None:
            delta = event.scenePos() - self._start_pos
            if self._resize_handle == "p1":
                self._p1 = self._start_p1 + delta
            else:
                self._p2 = self._start_p2 + delta
            self.prepareGeometryChange()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._resize_handle is not None:
            self._resize_handle = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def set_label(self, text):
        self._label = text
        self.update()

    def to_dict(self):
        return {
            "type": "ruler",
            "x1": self._p1.x(), "y1": self._p1.y(),
            "x2": self._p2.x(), "y2": self._p2.y(),
            "pos_x": self.pos().x(), "pos_y": self.pos().y(),
            "label": self._label,
            "font_size": self._font_size,
        }

    @classmethod
    def from_dict(cls, d):
        item = cls(QPointF(d.get("x1", 0), d.get("y1", 0)),
                   QPointF(d.get("x2", 100), d.get("y2", 0)),
                   d.get("label", ""), d.get("font_size", 40))
        item.setPos(d.get("pos_x", 0), d.get("pos_y", 0))
        return item


class CropRectItem(QGraphicsObject):
    HANDLE_R = 9

    def __init__(self, rect):
        super().__init__()
        self._rect = QRectF(rect)
        self._resize_handle = None
        self._start_rect = None
        self._start_pos = None
        self.setAcceptHoverEvents(True)
        self.setZValue(20)

    def boundingRect(self):
        s = self.HANDLE_R + 6
        return self._rect.adjusted(-s, -s, s, s)

    def paint(self, painter, option, widget):
        painter.setPen(QPen(QColor("#00d4ff"), 2, Qt.DashLine))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(self._rect)
        painter.setBrush(QBrush(QColor("#00d4ff")))
        painter.setPen(QPen(QColor("#0d1b2a"), 2))
        for c in self._corners():
            painter.drawEllipse(c, self.HANDLE_R, self.HANDLE_R)

    def _corners(self):
        r = self._rect
        return [QPointF(r.left(), r.top()),
                QPointF(r.right(), r.top()),
                QPointF(r.right(), r.bottom()),
                QPointF(r.left(), r.bottom())]

    def _hit_handle(self, pos):
        for i, c in enumerate(self._corners()):
            if (pos - c).manhattanLength() < 3 * self.HANDLE_R:
                return i
        return None

    def hoverMoveEvent(self, event):
        h = self._hit_handle(event.pos())
        cursors = [Qt.SizeFDiagCursor, Qt.SizeBDiagCursor,
                   Qt.SizeFDiagCursor, Qt.SizeBDiagCursor]
        if h is not None:
            self.setCursor(cursors[h])
        else:
            self.setCursor(Qt.SizeAllCursor)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            h = self._hit_handle(event.pos())
            if h is not None:
                self._resize_handle = h
                self._start_rect = QRectF(self._rect)
                self._start_pos = event.scenePos()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resize_handle is not None:
            delta = event.scenePos() - self._start_pos
            r = QRectF(self._start_rect)
            h = self._resize_handle
            if h == 0:
                r.setTopLeft(r.topLeft() + delta)
            elif h == 1:
                r.setTopRight(r.topRight() + delta)
            elif h == 2:
                r.setBottomRight(r.bottomRight() + delta)
            elif h == 3:
                r.setBottomLeft(r.bottomLeft() + delta)
            if r.width() < 20:
                r.setWidth(20)
            if r.height() < 20:
                r.setHeight(20)
            self.prepareGeometryChange()
            self._rect = r
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._resize_handle is not None:
            self._resize_handle = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def rect(self):
        return QRectF(self._rect)


# =====================================================================
#  Диалог шаблонов
# =====================================================================
class TemplatesDialog(QDialog):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Шаблоны описаний и заключений")
        self.setMinimumSize(900, 620)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        t_desc = QWidget()
        self._build_template_tab(t_desc, kind="description")
        tabs.addTab(t_desc, "📝 Шаблоны описания")

        t_concl = QWidget()
        self._build_template_tab(t_concl, kind="conclusion")
        tabs.addTab(t_concl, "📋 Шаблоны заключения")

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        layout.addWidget(bb)

    def _build_template_tab(self, parent, kind):
        v = QVBoxLayout(parent)
        split = QSplitter(Qt.Horizontal)
        v.addWidget(split, 1)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QLabel("Список шаблонов:"))

        lst = QListWidget()
        lst.setMinimumWidth(240)
        lv.addWidget(lst, 1)

        btns = QHBoxLayout()
        b_new = QPushButton("➕ Новый")
        b_new.clicked.connect(
            lambda: self._new_template(kind, lst, ed_name, ed_body))
        btns.addWidget(b_new)

        b_del = QPushButton("🗑 Удалить")
        b_del.clicked.connect(lambda: self._delete_template(kind, lst))
        btns.addWidget(b_del)
        lv.addLayout(btns)

        split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)

        hb = QHBoxLayout()
        hb.addWidget(QLabel("Название:"))
        ed_name = QLineEdit()
        hb.addWidget(ed_name, 1)
        rv.addLayout(hb)

        rv.addWidget(QLabel("Текст шаблона:"))
        ed_body = QTextEdit()
        ed_body.setFont(QFont("Consolas", 11))
        rv.addWidget(ed_body, 1)

        hb2 = QHBoxLayout()
        hb2.addStretch(1)
        b_save = QPushButton("💾 Сохранить шаблон")
        b_save.setObjectName("primary")
        b_save.setMinimumHeight(36)
        b_save.clicked.connect(
            lambda: self._save_template(kind, lst, ed_name, ed_body))
        hb2.addWidget(b_save)
        rv.addLayout(hb2)

        split.addWidget(right)
        split.setSizes([300, 600])

        self._reload_list(kind, lst)

        def on_select():
            it = lst.currentItem()
            if not it:
                return
            name = it.text()
            rows = (self.db.templates() if kind == "description"
                    else self.db.conclusion_templates())
            for r in rows:
                if r["name"] == name:
                    ed_name.setText(r["name"])
                    ed_body.setPlainText(r["body"] or "")
                    break
        lst.itemSelectionChanged.connect(on_select)

        ed_name.clear()
        ed_body.clear()

    def _reload_list(self, kind, lst):
        lst.clear()
        rows = (self.db.templates() if kind == "description"
                else self.db.conclusion_templates())
        for r in rows:
            lst.addItem(QListWidgetItem(r["name"]))

    def _new_template(self, kind, lst, ed_name, ed_body):
        ed_name.clear()
        ed_body.clear()
        ed_name.setFocus()
        lst.clearSelection()

    def _save_template(self, kind, lst, ed_name, ed_body):
        name = ed_name.text().strip()
        body = ed_body.toPlainText().strip()
        if not name:
            QMessageBox.warning(self, "Проверьте", "Укажите название шаблона.")
            return
        if not body:
            QMessageBox.warning(self, "Проверьте", "Введите текст шаблона.")
            return
        if kind == "description":
            self.db.save_template(name, body)
        else:
            self.db.save_conclusion_template(name, body)
        self._reload_list(kind, lst)
        for i in range(lst.count()):
            if lst.item(i).text() == name:
                lst.setCurrentRow(i)
                break
        QMessageBox.information(self, "Готово",
                                f"Шаблон «{name}» сохранён.")

    def _delete_template(self, kind, lst):
        it = lst.currentItem()
        if not it:
            return
        name = it.text()
        if QMessageBox.question(self, "Удалить",
                                f"Удалить шаблон «{name}»?",
                                QMessageBox.Yes | QMessageBox.No) \
                == QMessageBox.Yes:
            if kind == "description":
                self.db.delete_template(name)
            else:
                self.db.delete_conclusion_template(name)
            self._reload_list(kind, lst)


# =====================================================================
#  Диалог опций печати
# =====================================================================
class PrintDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Печать исследования — параметры")
        self.setMinimumWidth(480)
        self.result_data = None

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("<b>Что печатать:</b>"))
        self.chk_image = QCheckBox("Изображение (с аннотациями)")
        self.chk_image.setChecked(True)
        self.chk_desc = QCheckBox("Описание")
        self.chk_desc.setChecked(True)
        self.chk_concl = QCheckBox("Заключение")
        self.chk_concl.setChecked(True)
        layout.addWidget(self.chk_image)
        layout.addWidget(self.chk_desc)
        layout.addWidget(self.chk_concl)

        layout.addSpacing(10)
        layout.addWidget(QLabel("<b>Шапка:</b>"))
        self.chk_header = QCheckBox(
            "Название и адрес медучреждения")
        self.chk_header.setChecked(True)
        layout.addWidget(self.chk_header)

        self.chk_patient = QCheckBox(
            "ФИО пациента и дата исследования")
        self.chk_patient.setChecked(True)
        layout.addWidget(self.chk_patient)

        layout.addStretch(1)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Просмотр и печать…")
        bb.button(QDialogButtonBox.Cancel).setText("Отмена")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def _accept(self):
        self.result_data = {
            "image": self.chk_image.isChecked(),
            "desc": self.chk_desc.isChecked(),
            "concl": self.chk_concl.isChecked(),
            "header": self.chk_header.isChecked(),
            "patient": self.chk_patient.isChecked(),
        }
        self.accept()


# =====================================================================
#  Окно просмотра исследования
# =====================================================================
class ViewerWindow(QMainWindow):
    TOOL_PAN = "pan"
    TOOL_WINDOW = "window"
    TOOL_CROP = "crop"
    TOOL_SELECT = "select"
    TOOL_RULER = "ruler"
    TOOL_TEXT = "text"

    def __init__(self, db, study_ids, current_index, parent=None):
        super().__init__(parent)
        self.db = db
        self.study_ids = list(study_ids) if study_ids else []
        self.current_index = current_index if current_index is not None else 0

        self.study_id = None
        self.study = None
        self.patient = None

        self._thumb_cache = {}

        self._original_array = None
        self._pixel_spacing = None
        self.brightness = 0.0
        self.contrast = 1.0
        self.rotation = 0
        self.flip_h = False
        self.flip_v = False
        self.crop_rect = None
        self.current_pixmap = None

        self.current_tool = self.TOOL_PAN
        self._drag_start = None
        self._current_shape = None
        self._bc_start_pos = None
        self._bc_start_b = 0
        self._bc_start_c = 1.0

        self._crop_item = None
        self._annotations = []
        self.pixmap_item = None

        self.setWindowTitle("Просмотр исследования")
        self.setMinimumSize(1300, 800)
        icon_path = _icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(str(icon_path)))

        self._build_ui()
        self._load_current_study()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        nav = QHBoxLayout()
        nav.setSpacing(10)

        self.btn_prev = QPushButton("◀")
        self.btn_prev.setObjectName("navarrow")
        self.btn_prev.setToolTip("Предыдущий пациент/исследование (←)")
        self.btn_prev.clicked.connect(self.go_prev)
        nav.addWidget(self.btn_prev)

        info_wrap = QWidget()
        iw = QVBoxLayout(info_wrap)
        iw.setContentsMargins(0, 0, 0, 0)
        iw.setSpacing(2)

        self.lbl_info = QLabel("")
        self.lbl_info.setObjectName("patientInfo")
        self.lbl_info.setMinimumHeight(60)
        iw.addWidget(self.lbl_info)

        nav.addWidget(info_wrap, 1)

        self.lbl_position = QLabel("1 / 1")
        self.lbl_position.setObjectName("navPosition")
        nav.addWidget(self.lbl_position)

        self.btn_next = QPushButton("▶")
        self.btn_next.setObjectName("navarrow")
        self.btn_next.setToolTip("Следующий пациент/исследование (→)")
        self.btn_next.clicked.connect(self.go_next)
        nav.addWidget(self.btn_next)

        root.addLayout(nav)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        left_panel = QWidget()
        lp = QVBoxLayout(left_panel)
        lp.setContentsMargins(0, 0, 0, 0)
        lp.setSpacing(6)

        self.lbl_card_title = QLabel("Исследования пациента")
        self.lbl_card_title.setObjectName("cardTitle")
        lp.addWidget(self.lbl_card_title)

        self.lbl_card_subtitle = QLabel("0 исследований")
        self.lbl_card_subtitle.setObjectName("cardSubtitle")
        lp.addWidget(self.lbl_card_subtitle)

        self.study_list = QListWidget()
        self.study_list.setObjectName("studyCards")
        self.study_list.setViewMode(QListWidget.IconMode)
        self.study_list.setIconSize(QSize(130, 130))
        self.study_list.setGridSize(QSize(160, 190))
        self.study_list.setResizeMode(QListWidget.Adjust)
        self.study_list.setMovement(QListWidget.Static)
        self.study_list.setSpacing(4)
        self.study_list.setWordWrap(True)
        self.study_list.setTextElideMode(Qt.ElideNone)
        self.study_list.itemClicked.connect(self._on_card_clicked)
        lp.addWidget(self.study_list, 1)

        splitter.addWidget(left_panel)

        center = QWidget()
        cv = QVBoxLayout(center)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(6)

        tb = QHBoxLayout()
        tb.setSpacing(4)

        self.btn_pan = self._icon_tool_button(
            "🖐", "Панорама — перемещение изображения",
            self.TOOL_PAN, checked=True)
        self.btn_window = self._icon_tool_button(
            "🔆", "Яркость/Контраст:\n↔ яркость · ↕ контраст",
            self.TOOL_WINDOW)
        self.btn_crop = self._icon_tool_button(
            "✂", "Обрезка", self.TOOL_CROP)
        self.btn_select = self._icon_tool_button(
            "▣", "Выделение", self.TOOL_SELECT)
        self.btn_ruler = self._icon_tool_button(
            "📏", "Линейка", self.TOOL_RULER)
        self.btn_text = self._icon_tool_button(
            "🅣", "Надпись", self.TOOL_TEXT)

        for b in (self.btn_pan, self.btn_window, self.btn_crop,
                  self.btn_select, self.btn_ruler, self.btn_text):
            tb.addWidget(b)
        tb.addSpacing(10)

        self.btn_apply_crop = QPushButton("✔")
        self.btn_apply_crop.setObjectName("icon_primary")
        self.btn_apply_crop.setToolTip("Применить обрезку")
        self.btn_apply_crop.clicked.connect(self._apply_current_crop)
        self.btn_apply_crop.setVisible(False)
        tb.addWidget(self.btn_apply_crop)

        self.btn_cancel_crop = QPushButton("✖")
        self.btn_cancel_crop.setObjectName("icon_danger")
        self.btn_cancel_crop.setToolTip("Отменить рамку обрезки")
        self.btn_cancel_crop.clicked.connect(self._cancel_crop)
        self.btn_cancel_crop.setVisible(False)
        tb.addWidget(self.btn_cancel_crop)

        tb.addSpacing(16)

        b_zoom_in = self._icon_action_button(
            "➕", "Увеличить", lambda: self.view.scale(1.25, 1.25))
        tb.addWidget(b_zoom_in)

        b_zoom_out = self._icon_action_button(
            "➖", "Уменьшить", lambda: self.view.scale(0.8, 0.8))
        tb.addWidget(b_zoom_out)

        b_fit = self._icon_action_button(
            "⤢", "Вписать в окно", self.fit_in_view)
        tb.addWidget(b_fit)

        b_rot_l = self._icon_action_button(
            "↺", "Повернуть влево на 90°", lambda: self.rotate_image(-90))
        tb.addWidget(b_rot_l)

        b_rot_r = self._icon_action_button(
            "↻", "Повернуть вправо на 90°", lambda: self.rotate_image(90))
        tb.addWidget(b_rot_r)

        b_flip_h = self._icon_action_button(
            "⇋", "Отразить по горизонтали",
            lambda: self.flip_image(True, False))
        tb.addWidget(b_flip_h)

        b_flip_v = self._icon_action_button(
            "⇅", "Отразить по вертикали",
            lambda: self.flip_image(False, True))
        tb.addWidget(b_flip_v)

        b_reset = self._icon_action_button(
            "🔄", "Сбросить рабочее место", self.reset_workspace)
        tb.addWidget(b_reset)

        tb.addSpacing(16)

        b_print = QPushButton("🖨")
        b_print.setObjectName("icon")
        b_print.setToolTip("Печать с предварительным просмотром")
        b_print.clicked.connect(self.print_document)
        tb.addWidget(b_print)

        b_save = QPushButton("💾")
        b_save.setObjectName("icon_primary")
        b_save.setToolTip("Сохранить копию изображения с аннотациями")
        b_save.clicked.connect(self.save_edited_copy)
        tb.addWidget(b_save)

        tb.addStretch(1)
        cv.addLayout(tb)

        self.scene = QGraphicsScene()
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHints(QPainter.Antialiasing |
                                 QPainter.SmoothPixmapTransform)
        self.view.setDragMode(QGraphicsView.NoDrag)
        self.view.setBackgroundBrush(QBrush(QColor("#05080d")))
        self.view.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.view.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self.view.setMouseTracking(True)
        self.view.mousePressEvent = self._mouse_press
        self.view.mouseMoveEvent = self._mouse_move
        self.view.mouseReleaseEvent = self._mouse_release
        self.view.wheelEvent = self._wheel_zoom
        cv.addWidget(self.view, 1)

        splitter.addWidget(center)

        right = QGroupBox("Описание исследования")
        rv = QVBoxLayout(right)

        hb = QHBoxLayout()
        hb.addWidget(QLabel("Шаблон описания:"))
        self.cmb_template = QComboBox()
        self.cmb_template.setMinimumWidth(160)
        self._reload_templates()
        self.cmb_template.currentTextChanged.connect(self._on_template_change)
        hb.addWidget(self.cmb_template, 1)

        b_manage_tpl = QPushButton("📋")
        b_manage_tpl.setObjectName("icon")
        b_manage_tpl.setToolTip("Управление шаблонами")
        b_manage_tpl.setMinimumSize(38, 38)
        b_manage_tpl.setMaximumSize(38, 38)
        b_manage_tpl.setFont(QFont("Segoe UI Emoji", 14))
        b_manage_tpl.clicked.connect(self._open_templates_dialog)
        hb.addWidget(b_manage_tpl)

        b_save_tpl = QPushButton("💾")
        b_save_tpl.setObjectName("icon")
        b_save_tpl.setToolTip("Сохранить описание как шаблон")
        b_save_tpl.setMinimumSize(38, 38)
        b_save_tpl.setMaximumSize(38, 38)
        b_save_tpl.setFont(QFont("Segoe UI Emoji", 14))
        b_save_tpl.clicked.connect(self._save_as_template)
        hb.addWidget(b_save_tpl)
        rv.addLayout(hb)

        rv.addWidget(QLabel("Описание:"))
        self.txt_description = QTextEdit()
        self.txt_description.setMinimumHeight(200)
        self.txt_description.setFont(QFont("Consolas", 10))
        rv.addWidget(self.txt_description, 2)

        hb3 = QHBoxLayout()
        hb3.addWidget(QLabel("Шаблон заключения:"))
        self.cmb_conclusion = QComboBox()
        self.cmb_conclusion.setMinimumWidth(160)
        self._reload_conclusion_templates()
        self.cmb_conclusion.currentTextChanged.connect(
            self._on_conclusion_template_change)
        hb3.addWidget(self.cmb_conclusion, 1)

        b_save_concl_tpl = QPushButton("💾")
        b_save_concl_tpl.setObjectName("icon")
        b_save_concl_tpl.setToolTip("Сохранить заключение как шаблон")
        b_save_concl_tpl.setMinimumSize(38, 38)
        b_save_concl_tpl.setMaximumSize(38, 38)
        b_save_concl_tpl.setFont(QFont("Segoe UI Emoji", 14))
        b_save_concl_tpl.clicked.connect(self._save_conclusion_as_template)
        hb3.addWidget(b_save_concl_tpl)
        rv.addLayout(hb3)

        rv.addWidget(QLabel("Заключение:"))
        self.txt_conclusion = QTextEdit()
        self.txt_conclusion.setMinimumHeight(120)
        self.txt_conclusion.setFont(QFont("Consolas", 10))
        rv.addWidget(self.txt_conclusion, 1)

        hb2 = QHBoxLayout()
        b_save_desc = QPushButton("💾 Сохранить описание")
        b_save_desc.setObjectName("primary")
        b_save_desc.setMinimumHeight(36)
        b_save_desc.clicked.connect(self.save_description)
        hb2.addWidget(b_save_desc)

        b_clear = QPushButton("Очистить")
        b_clear.clicked.connect(self._clear_description)
        hb2.addWidget(b_clear)
        rv.addLayout(hb2)

        splitter.addWidget(right)
        splitter.setSizes([220, 900, 380])

        self.statusBar().showMessage(
            "← / → — листание · Клик по карточке — исследование пациента")

    # ----------------------------------------------------------------
    #  Получение файла исследования (все файлы в одной папке)
    # ----------------------------------------------------------------
    def _find_study_file(self, study_row):
        """
        Возвращает путь к DICOM-файлу исследования.
        Сначала пробуем file_path. Если это не файл — ищем в folder по UID.
        """
        # 1) file_path — если это файл, используем его
        fp = study_row["file_path"] if "file_path" in study_row.keys() else None
        if fp and os.path.isfile(fp):
            return fp

        # 2) folder + поиск по StudyInstanceUID
        folder = study_row["folder"] if "folder" in study_row.keys() else None
        if folder and os.path.isdir(folder):
            uid = study_row["study_uid"] if "study_uid" in study_row.keys() else None
            if uid:
                try:
                    files = sorted(
                        os.path.join(folder, f)
                        for f in os.listdir(folder)
                        if os.path.isfile(os.path.join(folder, f))
                    )
                except Exception:
                    files = []
                for full in files:
                    try:
                        ds = pydicom.dcmread(full, stop_before_pixels=True,
                                             force=True)
                        if str(getattr(ds, "StudyInstanceUID", "")) == uid:
                            return full
                    except Exception:
                        continue
            # если UID не задан — просто первый .dcm
            try:
                for f in sorted(os.listdir(folder)):
                    full = os.path.join(folder, f)
                    if os.path.isfile(full) and f.lower().endswith(".dcm"):
                        return full
            except Exception:
                pass

        # 3) file_path как папка (на всякий случай)
        if fp and os.path.isdir(fp):
            try:
                for f in sorted(os.listdir(fp)):
                    full = os.path.join(fp, f)
                    if os.path.isfile(full):
                        return full
            except Exception:
                pass

        return None

    def _get_study_thumbnail(self, study_row):
        sid = study_row["id"]
        if sid in self._thumb_cache:
            return self._thumb_cache[sid]

        pix = None
        path = self._find_study_file(study_row)
        if path and os.path.isfile(path):
            try:
                ds = pydicom.dcmread(path, force=True)
                arr = ds.pixel_array.astype(np.float32)
                mn, mx = float(arr.min()), float(arr.max())
                arr = ((arr - mn) / (mx - mn + 1e-9)) * 255.0
                arr = arr.astype(np.uint8)
                if str(getattr(ds, "PhotometricInterpretation", "")).upper() \
                        == "MONOCHROME1":
                    arr = 255 - arr
                arr = np.ascontiguousarray(arr)
                hh, ww = arr.shape
                data = arr.tobytes()
                img = QImage(data, ww, hh, ww,
                             QImage.Format_Grayscale8).copy()
                pix = QPixmap.fromImage(img)
            except Exception:
                pix = None

        if pix is None or pix.isNull():
            pix = QPixmap(130, 130)
            pix.fill(QColor("#1b263b"))
            p = QPainter(pix)
            p.setPen(QColor("#4a5568"))
            f = QFont("Segoe UI Emoji", 32)
            p.setFont(f)
            p.drawText(pix.rect(), Qt.AlignCenter, "📷")
            p.end()

        pix = pix.scaled(130, 130, Qt.KeepAspectRatio,
                         Qt.SmoothTransformation)
        self._thumb_cache[sid] = pix
        return pix

    def _rebuild_study_cards(self):
        self.study_list.blockSignals(True)
        self.study_list.clear()

        if not self.patient:
            self.study_list.blockSignals(False)
            return

        rows = self.db.studies_of_patient(self.patient["id"])
        for r in rows:
            thumb = self._get_study_thumbnail(r)
            date_s = r["study_date"] or "—"
            time_s = (r["study_time"] or "")[:5]
            label = f"{date_s}\n{time_s}" if time_s else date_s

            it = QListWidgetItem()
            it.setIcon(QIcon(thumb))
            it.setText(label)
            it.setData(Qt.UserRole, r["id"])
            it.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
            it.setToolTip(
                f"Дата: {date_s} {r['study_time'] or ''}\n"
                f"Модальность: {r['modality'] or '—'}\n"
                f"Область: {r['bodypart'] or '—'}"
            )
            self.study_list.addItem(it)

        for i in range(self.study_list.count()):
            it = self.study_list.item(i)
            if it.data(Qt.UserRole) == self.study_id:
                self.study_list.setCurrentItem(it)
                self.study_list.scrollToItem(
                    it, QAbstractItemView.PositionAtCenter)
                break

        self.study_list.blockSignals(False)

        self.lbl_card_subtitle.setText(
            f"Найдено: {len(rows)} · по дате (новые сверху)"
        )

    def _on_card_clicked(self, item):
        sid = item.data(Qt.UserRole)
        if sid is None or sid == self.study_id:
            return
        self._save_state()
        try:
            idx = self.study_ids.index(sid)
            self.current_index = idx
        except ValueError:
            self.study_ids.append(sid)
            self.current_index = len(self.study_ids) - 1
        self._load_current_study()

    def _update_position_label(self):
        total = len(self.study_ids)
        pos = self.current_index + 1 if total else 0
        self.lbl_position.setText(f"{pos} / {total}")
        self.btn_prev.setEnabled(self.current_index > 0)
        self.btn_next.setEnabled(self.current_index < total - 1)

    def go_prev(self):
        if self.current_index > 0:
            self._save_state()
            self.current_index -= 1
            self._load_current_study()

    def go_next(self):
        if self.current_index < len(self.study_ids) - 1:
            self._save_state()
            self.current_index += 1
            self._load_current_study()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Left:
            self.go_prev()
        elif event.key() == Qt.Key_Right:
            self.go_next()
        else:
            super().keyPressEvent(event)

    def _load_current_study(self):
        if not self.study_ids:
            QMessageBox.warning(self, "Ошибка", "Нет исследований для показа.")
            self.close()
            return

        self._annotations = []
        self._crop_item = None
        self._current_shape = None
        self._drag_start = None
        self.pixmap_item = None
        self._original_array = None
        self.current_pixmap = None
        self._pixel_spacing = None

        try:
            self.scene.clear()
        except Exception:
            pass

        self.brightness = 0.0
        self.contrast = 1.0
        self.rotation = 0
        self.flip_h = False
        self.flip_v = False
        self.crop_rect = None

        self.study_id = self.study_ids[self.current_index]
        self.study = self.db.get_study(self.study_id)
        if not self.study:
            QMessageBox.warning(self, "Ошибка",
                                f"Исследование ID={self.study_id} не найдено.")
            return
        self.patient = self.db.get_patient(self.study["patient_id"])
        if not self.patient:
            QMessageBox.warning(self, "Ошибка",
                                "Пациент для исследования не найден.")
            return

        self.setWindowTitle(
            f"Просмотр: {self.patient['last_name']} "
            f"{self.patient['first_name']} — "
            f"{self.study['study_date'] or ''}"
        )
        sex_str = "М" if self.patient["sex"] else "Ж"
        info_text = (
            f"<b style='color:#00d4ff;font-size:12pt;'>"
            f"{self.patient['last_name']} {self.patient['first_name']} "
            f"{self.patient['middle_name']}</b><br>"
            f"<span style='color:#8d99ae;'>"
            f"Пол: {sex_str} · "
            f"Дата рождения: {self.patient['dob'] or '—'} · "
            f"Исследование: {self.study['study_date'] or '—'} "
            f"{self.study['study_time'] or ''} · "
            f"Модальность: {self.study['modality'] or '—'} · "
            f"Область: {self.study['bodypart'] or '—'}"
            f"</span>"
        )
        self.lbl_info.setText(info_text)

        self.txt_description.blockSignals(True)
        self.txt_conclusion.blockSignals(True)
        self.txt_description.setPlainText(self.study["description"] or "")
        self.txt_conclusion.setPlainText(self.study["conclusion"] or "")
        self.txt_description.blockSignals(False)
        self.txt_conclusion.blockSignals(False)

        self._update_position_label()
        self._load_image()
        self._restore_state()
        self._update_crop_buttons()
        self._rebuild_study_cards()

    def _icon_tool_button(self, icon, tooltip, tool, checked=False):
        b = QPushButton(icon)
        b.setObjectName("icon")
        b.setToolTip(tooltip)
        b.setCheckable(True)
        b.setChecked(checked)
        b.setFont(QFont("Segoe UI Emoji", 16))
        b.clicked.connect(lambda _, t=tool: self._set_tool(t))
        return b

    def _icon_action_button(self, icon, tooltip, slot):
        b = QPushButton(icon)
        b.setObjectName("icon")
        b.setToolTip(tooltip)
        b.setFont(QFont("Segoe UI Emoji", 14))
        b.clicked.connect(slot)
        return b

    def _set_tool(self, tool):
        self.current_tool = tool
        for b, t in ((self.btn_pan, self.TOOL_PAN),
                     (self.btn_window, self.TOOL_WINDOW),
                     (self.btn_crop, self.TOOL_CROP),
                     (self.btn_select, self.TOOL_SELECT),
                     (self.btn_ruler, self.TOOL_RULER),
                     (self.btn_text, self.TOOL_TEXT)):
            b.setChecked(t == tool)
        if tool == self.TOOL_PAN:
            self.view.setDragMode(QGraphicsView.ScrollHandDrag)
        else:
            self.view.setDragMode(QGraphicsView.NoDrag)
        self.scene.clearSelection()

    def _update_crop_buttons(self):
        has_crop = self._crop_item is not None
        self.btn_apply_crop.setVisible(has_crop)
        self.btn_cancel_crop.setVisible(has_crop)

    def _apply_current_crop(self):
        if self._crop_item is None:
            return
        r = self._crop_item.rect()
        self.crop_rect = (r.x(), r.y(), r.width(), r.height())
        try:
            self.scene.removeItem(self._crop_item)
        except Exception:
            pass
        self._crop_item = None
        self._update_crop_buttons()
        self._rebuild_pixmap(fit=True)
        self.statusBar().showMessage(
            f"Обрезано: {int(r.width())} × {int(r.height())}")

    def _cancel_crop(self):
        if self._crop_item is not None:
            try:
                self.scene.removeItem(self._crop_item)
            except Exception:
                pass
            self._crop_item = None
        self._update_crop_buttons()
        self.statusBar().showMessage("Обрезка отменена.")

    def _load_image(self):
        # Ищем файл по UID/папке
        path = self._find_study_file(self.study)
        if path and os.path.isfile(path):
            self._load_file(path)
            return
        self._show_placeholder(
            "Файл исследования не найден.\n"
            f"Папка: {self.study['folder'] or '—'}\n"
            f"UID: {self.study['study_uid'] or '—'}"
        )

    def _load_file(self, path):
        try:
            ds = pydicom.dcmread(path, force=True)
            arr = ds.pixel_array.astype(np.float32)

            slope = float(getattr(ds, "RescaleSlope", 1) or 1)
            intercept = float(getattr(ds, "RescaleIntercept", 0) or 0)
            arr = arr * slope + intercept

            wc = getattr(ds, "WindowCenter", None)
            ww = getattr(ds, "WindowWidth", None)
            try:
                wc = float(wc[0] if isinstance(wc, (list, tuple)) else wc)
                ww = float(ww[0] if isinstance(ww, (list, tuple)) else ww)
            except Exception:
                wc = ww = None

            if wc is not None and ww is not None and ww > 0:
                lo = wc - ww / 2
                hi = wc + ww / 2
                arr = np.clip(arr, lo, hi)
                arr = (arr - lo) / (hi - lo) * 255.0
            else:
                mn, mx = float(arr.min()), float(arr.max())
                arr = ((arr - mn) / (mx - mn + 1e-9)) * 255.0

            arr = arr.astype(np.uint8)
            if str(getattr(ds, "PhotometricInterpretation", "")).upper() == \
                    "MONOCHROME1":
                arr = 255 - arr

            self._original_array = np.ascontiguousarray(arr)

            try:
                ps = ds.get("PixelSpacing", None) or \
                     ds.get("ImagerPixelSpacing", None)
                if ps:
                    self._pixel_spacing = (float(ps[0]), float(ps[1]))
            except Exception:
                self._pixel_spacing = None

            self._rebuild_pixmap(fit=True)
            self.statusBar().showMessage(
                f"Загружено: {os.path.basename(path)} · "
                f"{arr.shape[1]}×{arr.shape[0]} · "
                f"{'есть PixelSpacing' if self._pixel_spacing else 'без метрики'}"
            )
        except Exception as e:
            self._show_placeholder(f"Не удалось прочитать DICOM:\n{e}")

    def _rebuild_pixmap(self, fit=False):
        if self._original_array is None:
            return

        saved_transform = None
        saved_h = saved_v = 0
        if not fit:
            try:
                saved_transform = QTransform(self.view.transform())
                saved_h = self.view.horizontalScrollBar().value()
                saved_v = self.view.verticalScrollBar().value()
            except Exception:
                saved_transform = None

        arr = self._original_array.astype(np.float32)
        arr = (arr - 128.0) * self.contrast + 128.0 + self.brightness
        arr = np.clip(arr, 0, 255).astype(np.uint8)

        if self.crop_rect:
            x, y, w, h = self.crop_rect
            y1 = max(0, int(y))
            y2 = min(arr.shape[0], int(y + h))
            x1 = max(0, int(x))
            x2 = min(arr.shape[1], int(x + w))
            if y2 > y1 and x2 > x1:
                arr = arr[y1:y2, x1:x2]

        arr = np.ascontiguousarray(arr)
        hh, ww = arr.shape
        data = arr.tobytes()
        img = QImage(data, ww, hh, ww, QImage.Format_Grayscale8).copy()
        pix = QPixmap.fromImage(img)

        if self.rotation or self.flip_h or self.flip_v:
            tr = QTransform()
            if self.rotation:
                tr.rotate(self.rotation)
            if self.flip_h:
                tr.scale(-1, 1)
            if self.flip_v:
                tr.scale(1, -1)
            pix = pix.transformed(tr, Qt.SmoothTransformation)

        self.current_pixmap = pix
        self._display_pixmap(pix, saved_transform=saved_transform,
                             saved_h=saved_h, saved_v=saved_v, fit=fit)

    def _display_pixmap(self, pixmap, saved_transform=None,
                        saved_h=0, saved_v=0, fit=True):
        try:
            need_new = (self.pixmap_item is None
                        or self.pixmap_item.scene() is None)
        except (RuntimeError, AttributeError):
            need_new = True
            self.pixmap_item = None

        if need_new:
            self.pixmap_item = self.scene.addPixmap(pixmap)
            self.pixmap_item.setTransformationMode(Qt.SmoothTransformation)
            self.pixmap_item.setAcceptedMouseButtons(Qt.NoButton)
            self.pixmap_item.setZValue(-10)
        else:
            try:
                self.pixmap_item.setPixmap(pixmap)
                self.pixmap_item.setOffset(0, 0)
            except RuntimeError:
                self.pixmap_item = self.scene.addPixmap(pixmap)
                self.pixmap_item.setTransformationMode(
                    Qt.SmoothTransformation)
                self.pixmap_item.setAcceptedMouseButtons(Qt.NoButton)
                self.pixmap_item.setZValue(-10)

        self.scene.setSceneRect(QRectF(pixmap.rect()))

        if fit or saved_transform is None:
            self.fit_in_view()
        else:
            try:
                self.view.setTransform(saved_transform)
                self.view.horizontalScrollBar().setValue(saved_h)
                self.view.verticalScrollBar().setValue(saved_v)
            except Exception:
                self.fit_in_view()

    def _show_placeholder(self, text):
        self.pixmap_item = None
        self._annotations = []
        self._crop_item = None
        self._current_shape = None
        self.scene.clear()
        self._update_crop_buttons()
        t = self.scene.addText(text)
        t.setDefaultTextColor(QColor("#8d99ae"))
        t.setFont(QFont("Segoe UI", 14))
        self.scene.setSceneRect(t.boundingRect())

    def fit_in_view(self):
        try:
            if self.pixmap_item is None or self.pixmap_item.scene() is None:
                return
        except (RuntimeError, AttributeError):
            return
        self.view.fitInView(self.pixmap_item, Qt.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)

    def _wheel_zoom(self, event):
        if event.angleDelta().y() > 0:
            self.view.scale(1.15, 1.15)
        else:
            self.view.scale(0.87, 0.87)

    def _mouse_press(self, event):
        if self.current_pixmap is None:
            return QGraphicsView.mousePressEvent(self.view, event)
        if event.button() != Qt.LeftButton:
            return QGraphicsView.mousePressEvent(self.view, event)

        pos = self.view.mapToScene(event.pos())

        item = self.scene.itemAt(pos, self.view.transform())
        try:
            is_background = (item is None
                             or item is self.pixmap_item
                             or item is getattr(self, "pixmap_item", None))
        except Exception:
            is_background = True

        if not is_background:
            return QGraphicsView.mousePressEvent(self.view, event)

        self.scene.clearSelection()

        if self.current_tool == self.TOOL_PAN:
            return QGraphicsView.mousePressEvent(self.view, event)

        if self.current_tool == self.TOOL_WINDOW:
            self._bc_start_pos = event.pos()
            self._bc_start_b = self.brightness
            self._bc_start_c = self.contrast
            event.accept()
            return

        if self.current_tool == self.TOOL_TEXT:
            text, ok = QInputDialog.getText(self, "Надпись", "Введите текст:")
            if ok and text.strip():
                item = ResizableTextItem(text.strip(), pos)
                item.setZValue(5)
                self.scene.addItem(item)
                self._annotations.append(item)
            event.accept()
            return

        if self.current_tool == self.TOOL_RULER:
            self._drag_start = pos
            self._current_shape = QGraphicsLineItem(QLineF(pos, pos))
            self._current_shape.setPen(QPen(QColor("#ff4444"), 4))
            self._current_shape.setZValue(5)
            self.scene.addItem(self._current_shape)
            event.accept()
            return

        if self.current_tool == self.TOOL_SELECT:
            self._drag_start = pos
            self._current_shape = QGraphicsRectItem(QRectF(pos, pos))
            self._current_shape.setPen(QPen(QColor("#00d4ff"), 2, Qt.DashLine))
            self._current_shape.setZValue(5)
            self.scene.addItem(self._current_shape)
            event.accept()
            return

        if self.current_tool == self.TOOL_CROP:
            if self._crop_item is not None:
                try:
                    self.scene.removeItem(self._crop_item)
                except Exception:
                    pass
                self._crop_item = None
                self._update_crop_buttons()
            self._drag_start = pos
            self._current_shape = QGraphicsRectItem(QRectF(pos, pos))
            self._current_shape.setPen(QPen(QColor("#00d4ff"), 2, Qt.DashLine))
            self._current_shape.setZValue(20)
            self.scene.addItem(self._current_shape)
            event.accept()
            return

        return QGraphicsView.mousePressEvent(self.view, event)

    def _mouse_move(self, event):
        if self.current_tool == self.TOOL_WINDOW and \
                self._bc_start_pos is not None:
            dx = event.pos().x() - self._bc_start_pos.x()
            dy = event.pos().y() - self._bc_start_pos.y()
            self.brightness = max(-150.0, min(150.0,
                                              self._bc_start_b + dx * 0.5))
            self.contrast = max(0.2, min(4.0,
                                         self._bc_start_c - dy * 0.006))
            self._rebuild_pixmap(fit=False)
            self.statusBar().showMessage(
                f"Яркость: {self.brightness:+.0f}  "
                f"Контраст: {self.contrast:.2f}")
            event.accept()
            return

        if self._current_shape is None or self._drag_start is None:
            pos = self.view.mapToScene(event.pos())
            self.statusBar().showMessage(
                f"x={int(pos.x())}, y={int(pos.y())}")
            return QGraphicsView.mouseMoveEvent(self.view, event)

        pos = self.view.mapToScene(event.pos())

        try:
            if isinstance(self._current_shape, QGraphicsRectItem):
                rect = QRectF(self._drag_start, pos).normalized()
                self._current_shape.setRect(rect)
            elif isinstance(self._current_shape, QGraphicsLineItem):
                self._current_shape.setLine(QLineF(self._drag_start, pos))
        except RuntimeError:
            self._current_shape = None
            self._drag_start = None
            return

        event.accept()

    def _mouse_release(self, event):
        if self.current_tool == self.TOOL_WINDOW:
            self._bc_start_pos = None
            event.accept()
            return

        if self._current_shape is None:
            return QGraphicsView.mouseReleaseEvent(self.view, event)

        if self.current_tool == self.TOOL_RULER and \
                isinstance(self._current_shape, QGraphicsLineItem):
            try:
                line = self._current_shape.line()
            except RuntimeError:
                self._current_shape = None
                self._drag_start = None
                return

            length_px = ((line.x2() - line.x1()) ** 2 +
                         (line.y2() - line.y1()) ** 2) ** 0.5

            if length_px < 20:
                try:
                    self.scene.removeItem(self._current_shape)
                except Exception:
                    pass
                self._current_shape = None
                self._drag_start = None
                self.statusBar().showMessage(
                    "Линейка слишком короткая — отменено")
                event.accept()
                return

            auto_text = f"{int(length_px)} px"
            if self._pixel_spacing:
                try:
                    mm = length_px * float(self._pixel_spacing[0])
                    auto_text = f"{mm:.1f} мм"
                except Exception:
                    pass

            try:
                self.scene.removeItem(self._current_shape)
            except Exception:
                pass
            self._current_shape = None
            self._drag_start = None

            dlg = QDialog(self)
            dlg.setWindowTitle("Линейка")
            dlg.setMinimumWidth(420)
            dl = QVBoxLayout(dlg)
            dl.addWidget(QLabel("Измеренная длина:"))
            info = QLabel(
                f"<b style='color:#00d4ff;font-size:14pt;'>{auto_text}</b>")
            dl.addWidget(info)
            dl.addWidget(QLabel("Текст подписи (можно изменить):"))
            ed = QLineEdit(auto_text)
            ed.selectAll()
            dl.addWidget(ed)

            btns = QDialogButtonBox(
                QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            btns.accepted.connect(dlg.accept)
            btns.rejected.connect(dlg.reject)
            dl.addWidget(btns)

            if dlg.exec_() != QDialog.Accepted:
                self.statusBar().showMessage("Линейка отменена")
                event.accept()
                return

            final_label = ed.text().strip() or auto_text

            item = RulerItem(
                QPointF(line.x1(), line.y1()),
                QPointF(line.x2(), line.y2()),
                final_label, font_size=40)
            item.setZValue(6)
            self.scene.addItem(item)
            self._annotations.append(item)
            self.statusBar().showMessage(f"Линейка добавлена: {final_label}")
            event.accept()
            return

        if self.current_tool == self.TOOL_SELECT and \
                isinstance(self._current_shape, QGraphicsRectItem):
            try:
                r = self._current_shape.rect()
            except RuntimeError:
                self._current_shape = None
                self._drag_start = None
                return
            if r.width() < 5 or r.height() < 5:
                try:
                    self.scene.removeItem(self._current_shape)
                except Exception:
                    pass
            else:
                self._annotations.append(self._current_shape)
            self._current_shape = None
            self._drag_start = None
            event.accept()
            return

        if self.current_tool == self.TOOL_CROP and \
                isinstance(self._current_shape, QGraphicsRectItem):
            try:
                r = self._current_shape.rect()
            except RuntimeError:
                self._current_shape = None
                self._drag_start = None
                return
            try:
                self.scene.removeItem(self._current_shape)
            except Exception:
                pass
            self._current_shape = None
            self._drag_start = None
            if r.width() < 20 or r.height() < 20:
                event.accept()
                return
            self._crop_item = CropRectItem(r)
            self._crop_item.setZValue(20)
            self.scene.addItem(self._crop_item)
            self._update_crop_buttons()
            self.statusBar().showMessage(
                "Настройте рамку и нажмите «✔ Обрезать»")
            event.accept()
            return

        self._current_shape = None
        self._drag_start = None
        return QGraphicsView.mouseReleaseEvent(self.view, event)

    def rotate_image(self, angle):
        self.rotation = (self.rotation + angle) % 360
        self._rebuild_pixmap(fit=False)

    def flip_image(self, horiz, vert):
        if horiz:
            self.flip_h = not self.flip_h
        if vert:
            self.flip_v = not self.flip_v
        self._rebuild_pixmap(fit=False)

    def reset_workspace(self):
        try:
            ans = QMessageBox.question(
                self, "Сброс рабочего места",
                "Сбросить все преобразования изображения и удалить все надписи?",
                QMessageBox.Yes | QMessageBox.No)
            if ans != QMessageBox.Yes:
                return

            self._annotations = []
            self._crop_item = None
            self._current_shape = None
            self._drag_start = None
            self.pixmap_item = None

            try:
                self.scene.clear()
            except Exception:
                pass

            self.brightness = 0.0
            self.contrast = 1.0
            self.rotation = 0
            self.flip_h = False
            self.flip_v = False
            self.crop_rect = None

            self._update_crop_buttons()
            try:
                self.db.clear_viewer_state(self.study_id)
            except Exception:
                pass

            self._rebuild_pixmap(fit=True)
            self.statusBar().showMessage("Рабочее место сброшено.")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка сброса",
                                 f"Не удалось сбросить: {e}")

    def _serialize_annotations(self):
        result = []
        for a in self._annotations:
            try:
                if isinstance(a, (ResizableTextItem, RulerItem)):
                    result.append(a.to_dict())
                elif isinstance(a, QGraphicsRectItem):
                    r = a.rect()
                    result.append({
                        "type": "rect",
                        "x": r.x(), "y": r.y(),
                        "w": r.width(), "h": r.height(),
                    })
            except (RuntimeError, AttributeError):
                continue
        return result

    def _save_state(self):
        try:
            state = {
                "brightness": self.brightness,
                "contrast": self.contrast,
                "rotation": self.rotation,
                "flip_h": self.flip_h,
                "flip_v": self.flip_v,
                "crop_rect": list(self.crop_rect) if self.crop_rect else None,
                "annotations": self._serialize_annotations(),
            }
            self.db.set_viewer_state(
                self.study_id, json.dumps(state, ensure_ascii=False))
        except Exception:
            pass

    def _restore_state(self):
        try:
            raw = self.db.get_viewer_state(self.study_id)
            if not raw:
                return
            state = json.loads(raw)
        except Exception:
            return

        self.brightness = float(state.get("brightness", 0.0))
        self.contrast = float(state.get("contrast", 1.0))
        self.rotation = int(state.get("rotation", 0))
        self.flip_h = bool(state.get("flip_h", False))
        self.flip_v = bool(state.get("flip_v", False))
        cr = state.get("crop_rect")
        if cr:
            self.crop_rect = (cr[0], cr[1], cr[2], cr[3])

        self._rebuild_pixmap(fit=False)

        for a in state.get("annotations", []):
            t = a.get("type")
            try:
                if t == "text":
                    item = ResizableTextItem.from_dict(a)
                    item.setZValue(5)
                    self.scene.addItem(item)
                    self._annotations.append(item)
                elif t == "ruler":
                    item = RulerItem.from_dict(a)
                    item.setZValue(6)
                    self.scene.addItem(item)
                    self._annotations.append(item)
                elif t == "rect":
                    r = QGraphicsRectItem(QRectF(
                        a["x"], a["y"], a["w"], a["h"]))
                    r.setPen(QPen(QColor("#00d4ff"), 2, Qt.DashLine))
                    r.setZValue(5)
                    self.scene.addItem(r)
                    self._annotations.append(r)
            except Exception:
                continue

    def closeEvent(self, event):
        self._save_state()
        super().closeEvent(event)

    def save_edited_copy(self):
        if self.current_pixmap is None:
            return
        folder = self.study["folder"] or _app_dir() / "edited"
        if not folder or not os.path.isdir(folder):
            folder = str(_app_dir() / "edited")
            os.makedirs(folder, exist_ok=True)
        default_name = (f"{self.patient['last_name']}_"
                        f"{self.study['study_date'] or ''}_edited.png")
        default_name = re.sub(r'[<>:"/\\|?*]', "_", default_name)
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить копию", os.path.join(folder, default_name),
            "PNG (*.png);;JPEG (*.jpg);;BMP (*.bmp)")
        if not path:
            return
        try:
            self.scene.clearSelection()
            rect = self.scene.itemsBoundingRect()
            img = QImage(int(rect.width()), int(rect.height()),
                         QImage.Format_RGB32)
            img.fill(QColor("#000000"))
            painter = QPainter(img)
            self.scene.render(painter, QRectF(img.rect()), rect)
            painter.end()
            if img.save(path):
                QMessageBox.information(self, "Готово",
                                        f"Сохранено:\n{path}")
            else:
                QMessageBox.warning(self, "Ошибка", "Не удалось сохранить.")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", str(e))

    # ----------------------------------------------------------------
    #  Печать
    # ----------------------------------------------------------------
    def print_document(self):
        if not HAS_PRINT:
            QMessageBox.critical(
                self, "Ошибка",
                "Модуль печати PyQt5.QtPrintSupport недоступен.")
            return

        if self.current_pixmap is None and self.patient is None:
            QMessageBox.warning(self, "Нечего печатать",
                                "Нет ни изображения, ни данных пациента.")
            return

        opts_dlg = PrintDialog(self)
        if opts_dlg.exec_() != QDialog.Accepted or not opts_dlg.result_data:
            return
        opts = opts_dlg.result_data

        try:
            printer = QPrinter(QPrinter.HighResolution)
            printer.setPageSize(QPageSize(QPageSize.A4))
            printer.setPageOrientation(QPageLayout.Portrait)
            printer.setDocName(
                f"Печать исследования — "
                f"{self.patient['last_name']} "
                f"{self.study['study_date'] or ''}"
            )
            printer.setCreator("DICOM Viewer")
        except Exception as e:
            QMessageBox.critical(
                self, "Ошибка настройки принтера",
                f"Не удалось настроить принтер:\n{e}")
            return

        try:
            preview = QPrintPreviewDialog(printer, self)
            preview.setWindowTitle("Печать исследования — "
                                   "предварительный просмотр")
            preview.resize(1200, 850)
            preview.paintRequested.connect(
                lambda p: self._render_print(p, opts))
            preview.exec_()
        except Exception as e:
            QMessageBox.critical(
                self, "Ошибка печати",
                f"Не удалось открыть предварительный просмотр:\n{e}\n\n"
                f"{traceback.format_exc()}")

    def _measure_text_height(self, text, fm, max_w):
        if not text:
            return 0
        line_h = fm.height() + 2
        total_lines = 0
        for para in text.split("\n"):
            if not para.strip():
                total_lines += 1
                continue
            words = para.split(" ")
            line = ""
            for word in words:
                test = (line + " " + word).strip() if line else word
                if fm.horizontalAdvance(test) <= max_w:
                    line = test
                else:
                    total_lines += 1
                    line = word
            if line:
                total_lines += 1
        return total_lines * line_h

    def _render_print(self, printer, opts):
        painter = QPainter(printer)
        try:
            page_rect = printer.pageRect(QPrinter.DevicePixel)
            W = int(page_rect.width())
            H = int(page_rect.height())
            dpi = printer.resolution()
            margin = int(dpi * 10 / 25.4)
            x = margin
            avail_w = W - 2 * margin
            avail_h = H - 2 * margin

            font_small = QFont("Segoe UI", 9)
            font_normal = QFont("Segoe UI", 11)
            font_header = QFont("Segoe UI", 14); font_header.setBold(True)
            font_bold = QFont("Segoe UI", 11); font_bold.setBold(True)
            font_title = QFont("Segoe UI", 12); font_title.setBold(True)

            y = margin

            if opts["header"]:
                org_name = SETTINGS.value("org/name", "", type=str) or ""
                org_addr = SETTINGS.value("org/address", "", type=str) or ""
                org_phone = SETTINGS.value("org/phone", "", type=str) or ""

                if org_name:
                    painter.setFont(font_header)
                    painter.setPen(QColor(0, 0, 0))
                    fm = painter.fontMetrics()
                    th = fm.height()
                    painter.drawText(x, y + fm.ascent(), org_name)
                    y += th + 8

                if org_addr:
                    painter.setFont(font_small)
                    painter.setPen(QColor(60, 60, 60))
                    fm = painter.fontMetrics()
                    th = fm.height()
                    line = org_addr
                    if org_phone:
                        line += f"   ·   тел.: {org_phone}"
                    painter.drawText(x, y + fm.ascent(), line)
                    y += th + 10

                painter.setPen(QColor(180, 180, 180))
                painter.drawLine(x, y, W - margin, y)
                y += 15

            if opts["patient"] and self.patient:
                sex_str = "М" if self.patient["sex"] else "Ж"
                fio = (f"{self.patient['last_name']} "
                       f"{self.patient['first_name']} "
                       f"{self.patient['middle_name']}").strip()
                dob = self.patient["dob"] or "—"
                study_date = self.study["study_date"] or "—"
                study_time = self.study["study_time"] or ""
                modality = self.study["modality"] or "—"
                bodypart = self.study["bodypart"] or "—"

                painter.setFont(font_bold)
                painter.setPen(QColor(0, 0, 0))
                fm = painter.fontMetrics()
                th = fm.height()
                painter.drawText(x, y + fm.ascent(), f"Пациент: {fio}")
                y += th + 10

                painter.setFont(font_normal)
                fm = painter.fontMetrics()
                th = fm.height()
                painter.drawText(
                    x, y + fm.ascent(),
                    f"Пол: {sex_str}   ·   Дата рождения: {dob}   ·   "
                    f"Дата исследования: {study_date} {study_time}"
                )
                y += th + 6

                painter.drawText(
                    x, y + fm.ascent(),
                    f"Модальность: {modality}   ·   Область: {bodypart}"
                )
                y += th + 18

            painter.setFont(font_normal)
            fm_n = painter.fontMetrics()
            line_h = fm_n.height() + 2

            desc_text = ""
            concl_text = ""
            if opts["desc"]:
                desc_text = (self.txt_description.toPlainText() or "").strip()
            if opts["concl"]:
                concl_text = (self.txt_conclusion.toPlainText() or "").strip()

            painter.setFont(font_small)
            fm_s = painter.fontMetrics()
            footer_h = fm_s.height() + 8
            footer_y = H - margin - footer_h

            title_extra = int(line_h * 1.6)
            desc_h = (self._measure_text_height(desc_text, fm_n, avail_w)
                      + title_extra + 10) if desc_text else 0
            concl_h = (self._measure_text_height(concl_text, fm_n, avail_w)
                       + title_extra + 10) if concl_text else 0

            reserved_text_h = desc_h + concl_h
            max_text_h = int(avail_h * 0.45)
            if reserved_text_h > max_text_h:
                reserved_text_h = max_text_h

            text_top = footer_y - reserved_text_h - 10
            if text_top < y + 100:
                text_top = y + 100

            if opts["image"] and self.current_pixmap is not None:
                self.scene.clearSelection()
                src_rect = self.scene.itemsBoundingRect()
                if src_rect.width() > 0 and src_rect.height() > 0:
                    src_img = QImage(int(src_rect.width()),
                                     int(src_rect.height()),
                                     QImage.Format_RGB32)
                    src_img.fill(QColor("#000000"))
                    p2 = QPainter(src_img)
                    self.scene.render(p2, QRectF(src_img.rect()), src_rect)
                    p2.end()

                    img_top = y
                    img_bottom = text_top - 10
                    img_avail_h = img_bottom - img_top
                    if img_avail_h < 100:
                        img_avail_h = 100
                        img_bottom = img_top + 100

                    ratio = min(avail_w / src_img.width(),
                                img_avail_h / src_img.height())
                    w = int(src_img.width() * ratio)
                    h = int(src_img.height() * ratio)

                    ix = x + (avail_w - w) // 2
                    iy = img_top + (img_avail_h - h) // 2

                    painter.drawImage(QRectF(ix, iy, w, h), src_img)

            y = text_top
            if desc_text:
                painter.setFont(font_title)
                painter.setPen(QColor(0, 0, 0))
                fm_t = painter.fontMetrics()
                th_t = fm_t.height()
                painter.drawText(x, y + fm_t.ascent(), "Описание:")
                y += th_t + 6

                painter.setFont(font_normal)
                self._draw_wrapped_text(
                    painter, printer, desc_text,
                    x, y, avail_w,
                    footer_y - y - 10, margin)
                y = self._last_text_bottom + 8

            if concl_text:
                if y + int(line_h * 2) > footer_y:
                    pass
                else:
                    painter.setFont(font_title)
                    painter.setPen(QColor(0, 0, 0))
                    fm_t = painter.fontMetrics()
                    th_t = fm_t.height()
                    painter.drawText(x, y + fm_t.ascent(), "Заключение:")
                    y += th_t + 6

                    painter.setFont(font_normal)
                    self._draw_wrapped_text(
                        painter, printer, concl_text,
                        x, y, avail_w,
                        footer_y - y - 10, margin)

            painter.setFont(font_small)
            painter.setPen(QColor(60, 60, 60))
            fm_s = painter.fontMetrics()
            painter.drawText(
                x, footer_y + fm_s.ascent(),
                "Врач: ___________________________     "
                "Дата: " + datetime.date.today().strftime("%d.%m.%Y")
            )
        finally:
            painter.end()

    def _draw_wrapped_text(self, painter, printer, text,
                           x, y, max_w, max_h, margin):
        fm = painter.fontMetrics()
        line_h = fm.height() + 2

        paragraphs = text.split("\n")
        cur_y = y
        for para in paragraphs:
            words = para.split(" ")
            line = ""
            for word in words:
                test = (line + " " + word).strip() if line else word
                if fm.horizontalAdvance(test) <= max_w:
                    line = test
                else:
                    if cur_y + line_h > y + max_h:
                        printer.newPage()
                        cur_y = margin
                    painter.drawText(x, cur_y + fm.ascent(), line)
                    cur_y += line_h
                    line = word
            if line:
                if cur_y + line_h > y + max_h:
                    printer.newPage()
                    cur_y = margin
                painter.drawText(x, cur_y + fm.ascent(), line)
                cur_y += line_h

        self._last_text_bottom = cur_y + 6

    # ----------------------------------------------------------------
    #  Шаблоны
    # ----------------------------------------------------------------
    def _reload_templates(self):
        cur = self.cmb_template.currentText()
        self.cmb_template.blockSignals(True)
        self.cmb_template.clear()
        self.cmb_template.addItem("— без шаблона —", "")
        for t in self.db.templates():
            self.cmb_template.addItem(t["name"], t["body"])
        self.cmb_template.blockSignals(False)
        for i in range(self.cmb_template.count()):
            if self.cmb_template.itemText(i) == cur:
                self.cmb_template.setCurrentIndex(i)
                break

    def _reload_conclusion_templates(self):
        cur = self.cmb_conclusion.currentText()
        self.cmb_conclusion.blockSignals(True)
        self.cmb_conclusion.clear()
        self.cmb_conclusion.addItem("— без шаблона —", "")
        for t in self.db.conclusion_templates():
            self.cmb_conclusion.addItem(t["name"], t["body"])
        self.cmb_conclusion.blockSignals(False)
        for i in range(self.cmb_conclusion.count()):
            if self.cmb_conclusion.itemText(i) == cur:
                self.cmb_conclusion.setCurrentIndex(i)
                break

    def _on_template_change(self, name):
        body = self.cmb_template.currentData()
        if body and body != "":
            self.txt_description.setPlainText(body)

    def _on_conclusion_template_change(self, name):
        body = self.cmb_conclusion.currentData()
        if body and body != "":
            self.txt_conclusion.setPlainText(body)

    def _open_templates_dialog(self):
        dlg = TemplatesDialog(self.db, self)
        dlg.exec_()
        self._reload_templates()
        self._reload_conclusion_templates()

    def _save_as_template(self):
        text = self.txt_description.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Пусто",
                                "Введите текст описания перед сохранением.")
            return
        name, ok = QInputDialog.getText(self, "Сохранить как шаблон",
                                        "Название шаблона:")
        if ok and name.strip():
            self.db.save_template(name.strip(), text)
            self._reload_templates()
            QMessageBox.information(self, "Готово",
                                    f"Шаблон «{name}» сохранён.")

    def _save_conclusion_as_template(self):
        text = self.txt_conclusion.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Пусто",
                                "Введите текст заключения перед сохранением.")
            return
        name, ok = QInputDialog.getText(self, "Сохранить как шаблон заключения",
                                        "Название шаблона:")
        if ok and name.strip():
            self.db.save_conclusion_template(name.strip(), text)
            self._reload_conclusion_templates()
            QMessageBox.information(self, "Готово",
                                    f"Шаблон заключения «{name}» сохранён.")

    def _clear_description(self):
        if QMessageBox.question(self, "Очистить",
                                "Очистить поля описания?",
                                QMessageBox.Yes | QMessageBox.No) \
                == QMessageBox.Yes:
            self.txt_description.clear()
            self.txt_conclusion.clear()

    def save_description(self):
        desc = self.txt_description.toPlainText()
        concl = self.txt_conclusion.toPlainText()
        self.db.update_study_description(self.study_id, desc, concl)
        self._save_state()
        self.statusBar().showMessage("Описание сохранено.")
        QMessageBox.information(self, "Готово", "Описание сохранено в базе.")


# =====================================================================
#  Диалог редактирования пациента
# =====================================================================
class PatientDialog(QDialog):
    def __init__(self, db, patient_id, parent=None):
        super().__init__(parent)
        self.db = db
        self.patient_id = patient_id
        self.setWindowTitle("Редактирование пациента")
        self.setMinimumWidth(500)

        p = db.get_patient(patient_id)
        if not p:
            QMessageBox.warning(self, "Ошибка", "Пациент не найден")
            self.reject()
            return

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.ed_last = QLineEdit(p["last_name"] or "")
        self.ed_first = QLineEdit(p["first_name"] or "")
        self.ed_middle = QLineEdit(p["middle_name"] or "")
        self.ed_dob = QLineEdit(p["dob"] or "")
        self.ed_dob.setPlaceholderText("ГГГГ-ММ-ДД")
        self.cmb_sex = QComboBox()
        self.cmb_sex.addItems(["Женский", "Мужской"])
        self.cmb_sex.setCurrentIndex(1 if p["sex"] else 0)
        self.ed_phone = QLineEdit(p["phone"] or "")
        self.ed_address = QLineEdit(p["address"] or "")
        self.ed_snils = QLineEdit(p["snils"] or "")
        self.ed_policy = QLineEdit(p["policy"] or "")

        form.addRow("Фамилия:", self.ed_last)
        form.addRow("Имя:", self.ed_first)
        form.addRow("Отчество:", self.ed_middle)
        form.addRow("Дата рождения:", self.ed_dob)
        form.addRow("Пол:", self.cmb_sex)
        form.addRow("Телефон:", self.ed_phone)
        form.addRow("Адрес:", self.ed_address)
        form.addRow("СНИЛС:", self.ed_snils)
        form.addRow("Полис:", self.ed_policy)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self):
        last = self.ed_last.text().strip()
        if not last:
            QMessageBox.warning(self, "Проверьте", "Фамилия обязательна.")
            return
        self.db.upsert_patient(
            self.patient_id, last,
            self.ed_first.text().strip(),
            self.ed_middle.text().strip(),
            self.ed_dob.text().strip(),
            1 if self.cmb_sex.currentIndex() == 1 else 0,
            self.ed_phone.text().strip(),
            self.ed_address.text().strip(),
        )
        self.db.conn.execute("""UPDATE patients
            SET snils=?, policy=? WHERE id=?""",
            (self.ed_snils.text().strip(),
             self.ed_policy.text().strip(),
             self.patient_id))
        self.db.conn.commit()
        self.accept()


# =====================================================================
#  Диалог настроек
# =====================================================================
class SettingsDialog(QDialog):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Настройки")
        self.setMinimumSize(950, 720)
        self.resize(1000, 760)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        t_org = QWidget()
        self._build_org_tab(t_org)
        tabs.addTab(t_org, "🏥 Медучреждение")

        t_dicom = QWidget()
        self._build_dicom_tab(t_dicom)
        tabs.addTab(t_dicom, "📡 DICOM-сервер (Комета)")

        t_db = QWidget()
        self._build_db_tab(t_db)
        tabs.addTab(t_db, "💾 База данных")

        t_info = QWidget()
        il2 = QVBoxLayout(t_info)
        il2.addWidget(QLabel(
            "<h3 style='color:#00d4ff;'>Просмотрщик DICOM</h3>"
            "<p>Версия 2.5</p>"
            "<p>Работает с SQLite и .mdb Access.</p>"
            "<p>Печать с предварительным просмотром, "
            "проверка DICOM-сервера, шаблоны описаний и заключений.</p>"
        ))
        il2.addStretch(1)
        tabs.addTab(t_info, "ℹ О программе")

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText("💾 Сохранить")
        bb.button(QDialogButtonBox.Cancel).setText("Отмена")
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def _build_org_tab(self, parent):
        v = QVBoxLayout(parent)

        box_org = QGroupBox("Информация о медучреждении")
        g = QGridLayout(box_org)
        g.setColumnStretch(1, 1)
        g.setVerticalSpacing(10)

        g.addWidget(QLabel("Наименование:"), 0, 0)
        self.ed_org_name = QLineEdit(
            str(SETTINGS.value("org/name", "", type=str)))
        self.ed_org_name.setPlaceholderText(
            "Например: ГБУЗ «Городская поликлиника №1»")
        g.addWidget(self.ed_org_name, 0, 1, 1, 2)

        g.addWidget(QLabel("Адрес:"), 1, 0)
        self.ed_org_addr = QLineEdit(
            str(SETTINGS.value("org/address", "", type=str)))
        self.ed_org_addr.setPlaceholderText(
            "Например: г. Москва, ул. Ленина, д. 5")
        g.addWidget(self.ed_org_addr, 1, 1, 1, 2)

        g.addWidget(QLabel("Телефон:"), 2, 0)
        self.ed_org_phone = QLineEdit(
            str(SETTINGS.value("org/phone", "", type=str)))
        self.ed_org_phone.setPlaceholderText("+7 (495) 123-45-67")
        g.addWidget(self.ed_org_phone, 2, 1, 1, 2)

        g.addWidget(QLabel("Главный врач (ФИО):"), 3, 0)
        self.ed_org_doctor = QLineEdit(
            str(SETTINGS.value("org/chief", "", type=str)))
        g.addWidget(self.ed_org_doctor, 3, 1, 1, 2)

        v.addWidget(box_org)

        lbl_hint = QLabel(
            "ℹ Название и адрес выводятся в шапке документа при печати."
        )
        lbl_hint.setStyleSheet("color: #8d99ae; padding: 8px;")
        lbl_hint.setWordWrap(True)
        v.addWidget(lbl_hint)

        v.addStretch(1)

    def _build_dicom_tab(self, parent):
        v = QVBoxLayout(parent)

        box_server = QGroupBox("Сервер Комета (PACS)")
        g = QGridLayout(box_server)
        g.setColumnStretch(1, 1)
        g.setVerticalSpacing(10)

        g.addWidget(QLabel("IP-адрес:"), 0, 0)
        self.ed_srv_ip = QLineEdit(
            str(SETTINGS.value("dicom/host", "", type=str)))
        self.ed_srv_ip.setPlaceholderText("192.168.1.100")
        g.addWidget(self.ed_srv_ip, 0, 1, 1, 2)

        g.addWidget(QLabel("Порт:"), 1, 0)
        self.sp_srv_port = QSpinBox()
        self.sp_srv_port.setRange(1, 65535)
        self.sp_srv_port.setValue(
            int(SETTINGS.value("dicom/port", 104, type=int)))
        g.addWidget(self.sp_srv_port, 1, 1)
        g.addWidget(QLabel("(стандартный 104, часто 11112)"),
                     1, 2)

        g.addWidget(QLabel("AE Title сервера:"), 2, 0)
        self.ed_srv_ae = QLineEdit(
            str(SETTINGS.value("dicom/remote_ae", "KOMETA", type=str)))
        self.ed_srv_ae.setPlaceholderText("KOMETA")
        self.ed_srv_ae.setMaxLength(16)
        g.addWidget(self.ed_srv_ae, 2, 1, 1, 2)

        g.addWidget(QLabel("Наш AE Title:"), 3, 0)
        self.ed_our_ae = QLineEdit(
            str(SETTINGS.value("dicom/our_ae", "DICOMVIEWER", type=str)))
        self.ed_our_ae.setPlaceholderText("DICOMVIEWER")
        self.ed_our_ae.setMaxLength(16)
        g.addWidget(self.ed_our_ae, 3, 1, 1, 2)

        g.addWidget(QLabel("Таймаут (сек):"), 4, 0)
        self.sp_timeout = QSpinBox()
        self.sp_timeout.setRange(1, 60)
        self.sp_timeout.setValue(
            int(SETTINGS.value("dicom/timeout", 5, type=int)))
        g.addWidget(self.sp_timeout, 4, 1)
        v.addWidget(box_server)

        hb = QHBoxLayout()
        self.btn_check = QPushButton("🔍 Проверить подключение")
        self.btn_check.setObjectName("primary")
        self.btn_check.setMinimumHeight(42)
        self.btn_check.clicked.connect(self._check_connection)
        hb.addWidget(self.btn_check)
        hb.addStretch(1)
        v.addLayout(hb)

        self.lbl_check_result = QLabel("")
        self.lbl_check_result.setWordWrap(True)
        self.lbl_check_result.setStyleSheet(
            "background-color: #111d2e; border: 1px solid #2d3e50; "
            "border-radius: 6px; padding: 10px; color: #e0e1dd;")
        self.lbl_check_result.setMinimumHeight(80)
        v.addWidget(self.lbl_check_result)

        info = QLabel(
            "ℹ Проверка выполняет DICOM C-ECHO-запрос к серверу\n"
            "(если установлен модуль <b>pynetdicom</b>) либо простую "
            "TCP-проверку порта.\n\n"
            "Установка: <code>pip install pynetdicom</code>"
        )
        info.setStyleSheet("color: #8d99ae; padding: 6px;")
        info.setWordWrap(True)
        v.addWidget(info)

        v.addStretch(1)

    def _check_connection(self):
        ip = self.ed_srv_ip.text().strip()
        port = self.sp_srv_port.value()
        remote_ae = self.ed_srv_ae.text().strip()
        our_ae = self.ed_our_ae.text().strip()
        timeout = self.sp_timeout.value()

        self.btn_check.setEnabled(False)
        self.btn_check.setText("⏳ Проверка…")
        QApplication.processEvents()

        try:
            ok, msg = check_dicom_server(ip, port, our_ae, remote_ae,
                                         timeout=timeout)
        except Exception as e:
            ok, msg = False, f"Внутренняя ошибка проверки: {e}"
        finally:
            self.btn_check.setEnabled(True)
            self.btn_check.setText("🔍 Проверить подключение")

        if ok:
            self.lbl_check_result.setStyleSheet(
                "background-color: #0a2218; border: 1px solid #06d6a0; "
                "border-radius: 6px; padding: 10px; color: #06d6a0; "
                "font-weight: 600;")
            self.lbl_check_result.setText("✔ УСПЕШНО\n\n" + msg)
        else:
            self.lbl_check_result.setStyleSheet(
                "background-color: #2a1015; border: 1px solid #ef476f; "
                "border-radius: 6px; padding: 10px; color: #ef476f; "
                "font-weight: 600;")
            self.lbl_check_result.setText("✘ ОШИБКА\n\n" + msg)

    def _build_db_tab(self, parent):
        v = QVBoxLayout(parent)

        box_mdb = QGroupBox("Файл базы данных .mdb (основная)")
        g = QGridLayout(box_mdb)
        g.setColumnStretch(1, 1)
        g.addWidget(QLabel("Файл:"), 0, 0)
        self.ed_mdb = QLineEdit(
            str(SETTINGS.value("db/mdb_path", "", type=str)))
        g.addWidget(self.ed_mdb, 0, 1)
        b_mdb = QPushButton("Обзор…")
        b_mdb.clicked.connect(self._pick_mdb)
        g.addWidget(b_mdb, 0, 2)
        v.addWidget(box_mdb)

        box_storage = QGroupBox("Локальное хранилище снимков (одна папка)")
        g2 = QGridLayout(box_storage)
        g2.setColumnStretch(1, 1)
        g2.addWidget(QLabel("Папка:"), 0, 0)
        self.ed_storage = QLineEdit(
            str(SETTINGS.value("db/storage",
                               str(_app_dir() / "storage"), type=str)))
        g2.addWidget(self.ed_storage, 0, 1)
        b_st = QPushButton("Обзор…")
        b_st.clicked.connect(self._pick_storage)
        g2.addWidget(b_st, 0, 2)
        lbl_st = QLabel(
            "ℹ Все DICOM-файлы сохраняются в эту папку плоско,\n"
            "имя файла — по пациенту: ФАМИЛИЯ ИО + дата/время.dcm"
        )
        lbl_st.setStyleSheet("color: #8d99ae; padding: 4px;")
        lbl_st.setWordWrap(True)
        g2.addWidget(lbl_st, 1, 0, 1, 3)
        v.addWidget(box_storage)

        box_sync = QGroupBox("Синхронизация с .mdb")
        sl = QVBoxLayout(box_sync)
        b_sync = QPushButton("⟳ Синхронизировать SQLite из .mdb")
        b_sync.setMinimumHeight(36)
        b_sync.clicked.connect(self._sync_from_mdb)
        sl.addWidget(b_sync)
        lbl = QLabel(
            "Читает все исследования из .mdb и добавляет недостающие "
            "в SQLite. Файлы .dcm при этом НЕ копируются."
        )
        lbl.setStyleSheet("color: #8d99ae;")
        lbl.setWordWrap(True)
        sl.addWidget(lbl)
        v.addWidget(box_sync)

        box_restore = QGroupBox("Восстановление SQLite из папки со снимками")
        rl = QVBoxLayout(box_restore)
        b_restore = QPushButton("📂 Импорт из папки со снимками")
        b_restore.setObjectName("primary")
        b_restore.setMinimumHeight(40)
        b_restore.clicked.connect(self._restore_from_files)
        rl.addWidget(b_restore)
        lbl2 = QLabel(
            "Сканирует папку с DICOM-файлами, читает метаданные, "
            "копирует файлы в локальное хранилище "
            "и добавляет записи в SQLite.\n"
            "Уже импортированные (по StudyInstanceUID) — пропускаются.\n"
            "Используйте, если SQLite повреждён, а снимки остались."
        )
        lbl2.setStyleSheet("color: #8d99ae;")
        lbl2.setWordWrap(True)
        rl.addWidget(lbl2)
        v.addWidget(box_restore)

        box_info = QGroupBox("Информация о базе")
        il = QVBoxLayout(box_info)
        cnt_p = self.db.conn.execute(
            "SELECT COUNT(*) FROM patients").fetchone()[0]
        cnt_s = self.db.conn.execute(
            "SELECT COUNT(*) FROM studies").fetchone()[0]
        cnt_t = self.db.conn.execute(
            "SELECT COUNT(*) FROM templates").fetchone()[0]
        cnt_ct = self.db.conn.execute(
            "SELECT COUNT(*) FROM conclusion_templates").fetchone()[0]
        lbl_info = QLabel(
            f"Пациентов: <b>{cnt_p}</b><br>"
            f"Исследований: <b>{cnt_s}</b><br>"
            f"Шаблонов описаний: <b>{cnt_t}</b><br>"
            f"Шаблонов заключений: <b>{cnt_ct}</b><br>"
            f"Путь SQLite: <span style='color:#8d99ae;'>{self.db.path}</span>"
        )
        il.addWidget(lbl_info)
        v.addWidget(box_info)

        v.addStretch(1)

    def _pick_mdb(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Файл .mdb", self.ed_mdb.text() or "",
            "MS Access (*.mdb);;Все файлы (*)")
        if f:
            self.ed_mdb.setText(f)

    def _pick_storage(self):
        d = QFileDialog.getExistingDirectory(
            self, "Папка для хранения снимков",
            self.ed_storage.text() or "")
        if d:
            self.ed_storage.setText(d)

    def _save(self):
        SETTINGS.setValue("org/name", self.ed_org_name.text().strip())
        SETTINGS.setValue("org/address", self.ed_org_addr.text().strip())
        SETTINGS.setValue("org/phone", self.ed_org_phone.text().strip())
        SETTINGS.setValue("org/chief", self.ed_org_doctor.text().strip())

        SETTINGS.setValue("dicom/host", self.ed_srv_ip.text().strip())
        SETTINGS.setValue("dicom/port", self.sp_srv_port.value())
        SETTINGS.setValue("dicom/remote_ae", self.ed_srv_ae.text().strip())
        SETTINGS.setValue("dicom/our_ae", self.ed_our_ae.text().strip())
        SETTINGS.setValue("dicom/timeout", self.sp_timeout.value())

        SETTINGS.setValue("db/mdb_path", self.ed_mdb.text().strip())
        SETTINGS.setValue("db/storage", self.ed_storage.text().strip())

        SETTINGS.sync()
        self.accept()

    def _sync_from_mdb(self):
        """Синхронизация SQLite из .mdb — только записи, без файлов."""
        mdb = self.ed_mdb.text().strip()
        if not mdb or not os.path.isfile(mdb):
            QMessageBox.warning(self, "Проверьте", "Укажите .mdb-файл.")
            return
        if not HAS_ACCESS_PARSER:
            QMessageBox.critical(self, "Ошибка", "Нет access-parser.")
            return
        try:
            reader = AccessParser(mdb)
            pats = reader.parse_table("Patients")
            studs = reader.parse_table("studies")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка чтения .mdb", str(e))
            return

        added_p = added_s = 0
        if isinstance(pats, dict):
            def g(name):
                for k in pats.keys():
                    if str(k).lower() == name.lower():
                        return pats[k]
                return []
            ids = g("ID"); lastn = g("LastName"); firstn = g("FirstName")
            middlen = g("MidleName"); dobs = g("DOB"); sexes = g("Sex")
            n = len(ids)
            for i in range(n):
                try:
                    pid = int(float(ids[i]))
                except Exception:
                    continue
                last = fix_cp1251(str(lastn[i] or "")) if i < len(lastn) else ""
                first = fix_cp1251(str(firstn[i] or "")) if i < len(firstn) else ""
                middle = fix_cp1251(str(middlen[i] or "")) if i < len(middlen) else ""
                dob = ""
                if i < len(dobs) and dobs[i]:
                    try:
                        if isinstance(dobs[i], datetime.datetime):
                            dob = dobs[i].strftime("%Y-%m-%d")
                        else:
                            dob = str(dobs[i])[:10]
                    except Exception:
                        pass
                sex = 0
                try:
                    if i < len(sexes):
                        sex = int(float(sexes[i]))
                except Exception:
                    pass
                if self.db.get_patient(pid) is None:
                    self.db.upsert_patient(pid, last, first, middle, dob, sex)
                    added_p += 1

        if isinstance(studs, dict):
            def gs(name):
                for k in studs.keys():
                    if str(k).lower() == name.lower():
                        return studs[k]
                return []
            s_ids = gs("id"); s_uids = gs("study_uid")
            s_dates = gs("start_date"); s_pats = gs("patient_id")
            s_bp = gs("bodypart")
            n = len(s_ids)
            for i in range(n):
                uid = str(s_uids[i] or "").strip() if i < len(s_uids) else ""
                if not uid or self.db.study_exists(uid):
                    continue
                try:
                    pid = int(float(s_pats[i]))
                except Exception:
                    continue
                date_str = ""
                if i < len(s_dates) and s_dates[i]:
                    try:
                        if isinstance(s_dates[i], datetime.datetime):
                            date_str = s_dates[i].strftime("%Y-%m-%d")
                        else:
                            date_str = str(s_dates[i])[:10]
                    except Exception:
                        pass
                bp = str(s_bp[i] or "") if i < len(s_bp) else ""
                self.db.add_study(pid, uid, date_str, "", "", bp)
                added_s += 1

        QMessageBox.information(
            self, "Синхронизация завершена",
            f"Добавлено пациентов: {added_p}\n"
            f"Добавлено исследований: {added_s}\n\n"
            "ℹ DICOM-файлы НЕ копировались.\n"
            "Для копирования файлов используйте «Импорт из папки со снимками».")

    def _restore_from_files(self):
        """
        Восстановление / импорт из папки со снимками.
        Сканирует DICOM, копирует файлы в local_storage (плоско,
        с именем по пациенту) и создаёт записи в SQLite.
        Уже импортированные (по StudyInstanceUID) — пропускаются.
        """
        folder = QFileDialog.getExistingDirectory(
            self, "Папка с DICOM-файлами (источник)", "")
        if not folder:
            return
        if not HAS_PYDICOM:
            QMessageBox.critical(self, "Ошибка", "Нет pydicom.")
            return

        local_storage = self.ed_storage.text().strip()
        if not local_storage:
            QMessageBox.warning(
                self, "Проверьте",
                "Укажите папку локального хранилища в настройках.")
            return

        # Сканируем источник
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        studies = scan_dicom_folder(folder)
        QApplication.restoreOverrideCursor()

        if not studies:
            QMessageBox.information(
                self, "Ничего не найдено",
                f"В папке не найдено ни одного DICOM-файла:\n{folder}")
            return

        total_studies = len(studies)
        total_files = sum(len(v["files"]) for v in studies.values())

        added_p = added_s = 0
        skipped = 0
        files_copied = 0
        errors = []

        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()

        try:
            for uid, info in studies.items():
                m = info["meta"]
                # Пропускаем уже импортированные
                if self.db.study_exists(uid):
                    skipped += 1
                    continue

                # Пациент
                pid = self.db.find_patient(
                    m["family"], m["given"], m["dob"])
                if pid is None:
                    pid = self.db.max_patient_id() + 1
                    self.db.upsert_patient(pid, m["family"], m["given"],
                                            m["middle"], m["dob"], m["sex"])
                    added_p += 1

                # Копируем файлы в local_storage (плоско, с именем по пациенту)
                first_path, copied = copy_study_files(
                    info["files"], m, local_storage)
                files_copied += copied

                # Запись в БД
                self.db.add_study(
                    pid, uid, m["study_date"], m["study_time"],
                    m["modality"], m["bodypart"],
                    file_path=first_path,
                    folder=str(local_storage))
                added_s += 1
        except Exception as e:
            errors.append(str(e))
        finally:
            QApplication.restoreOverrideCursor()

        # Итоговый отчёт
        text = (
            f"Источник: {folder}\n"
            f"Хранилище: {local_storage}\n\n"
            f"Всего DICOM-файлов в источнике: {total_files}\n"
            f"Найдено исследований:          {total_studies}\n\n"
            f"Импортировано исследований:     {added_s}\n"
            f"Скопировано файлов:             {files_copied}\n"
            f"Новых пациентов:                {added_p}\n"
            f"Пропущено (уже в базе):         {skipped}\n"
        )
        if errors:
            text += f"\nОшибок: {len(errors)}\n"
            text += "\n".join(errors[:5])

        box = QMessageBox(self)
        box.setWindowTitle("Импорт из папки завершён")
        box.setIcon(QMessageBox.Warning if errors
                    else QMessageBox.Information)
        box.setText(text)
        box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.exec_()


# =====================================================================
#  Главное окно
# =====================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Просмотрщик DICOM-исследований")
        self.setMinimumSize(1300, 800)
        self.resize(1500, 900)

        icon_path = _icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(str(icon_path)))

        db_path = str(SETTINGS.value(
            "db/sqlite_path", str(_app_dir() / "viewer.db"), type=str))
        self.db = Database(db_path)

        self.import_worker = None
        self.current_filter = "all"
        self._current_study_ids = []
        self.viewer = None

        self._build_ui()
        self._load_studies()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        left = QWidget()
        left.setFixedWidth(230)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(6)

        lbl_logo = QLabel("⚕  DICOM Viewer")
        lbl_logo.setObjectName("title")
        lv.addWidget(lbl_logo)

        self.btn_nav_all = QPushButton("📁 Все исследования")
        self.btn_nav_all.setObjectName("nav")
        self.btn_nav_all.setCheckable(True)
        self.btn_nav_all.setChecked(True)
        self.btn_nav_all.clicked.connect(lambda: self._set_filter_mode("all"))

        self.btn_nav_undescr = QPushButton("📝 Для описания")
        self.btn_nav_undescr.setObjectName("nav")
        self.btn_nav_undescr.setCheckable(True)
        self.btn_nav_undescr.clicked.connect(
            lambda: self._set_filter_mode("undescribed"))

        self.btn_nav_patients = QPushButton("👥 Пациенты")
        self.btn_nav_patients.setObjectName("nav")
        self.btn_nav_patients.setCheckable(True)
        self.btn_nav_patients.clicked.connect(
            lambda: self._set_filter_mode("patients"))

        self.btn_nav_import = QPushButton("📥 Импорт с диска")
        self.btn_nav_import.setObjectName("nav")
        self.btn_nav_import.clicked.connect(self.import_from_disk)

        self.btn_nav_settings = QPushButton("⚙ Настройки")
        self.btn_nav_settings.setObjectName("nav")
        self.btn_nav_settings.clicked.connect(self.open_settings)

        for b in (self.btn_nav_all, self.btn_nav_undescr,
                  self.btn_nav_patients):
            lv.addWidget(b)
        lv.addSpacing(20)
        lv.addWidget(self.btn_nav_import)
        lv.addWidget(self.btn_nav_settings)
        lv.addStretch(1)

        self.lbl_counts = QLabel()
        self.lbl_counts.setObjectName("subtitle")
        self.lbl_counts.setWordWrap(True)
        lv.addWidget(self.lbl_counts)

        root.addWidget(left)

        center = QWidget()
        cv = QVBoxLayout(center)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(8)

        box_filter = QGroupBox("Поиск")
        fg = QGridLayout(box_filter)
        fg.setHorizontalSpacing(10)

        fg.addWidget(QLabel("Поиск:"), 0, 0)
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText(
            "ФИО, ИД пациента, StudyInstanceUID, описание…")
        self.ed_search.textChanged.connect(self._apply_filters)
        fg.addWidget(self.ed_search, 0, 1, 1, 5)

        fg.addWidget(QLabel("Дата с:"), 1, 0)
        self.ed_date_from = QDateEdit()
        self.ed_date_from.setCalendarPopup(True)
        self.ed_date_from.setDate(QDate(2000, 1, 1))
        self.ed_date_from.dateChanged.connect(self._apply_filters)
        fg.addWidget(self.ed_date_from, 1, 1)

        fg.addWidget(QLabel("по:"), 1, 2)
        self.ed_date_to = QDateEdit()
        self.ed_date_to.setCalendarPopup(True)
        self.ed_date_to.setDate(QDate.currentDate().addYears(1))
        self.ed_date_to.dateChanged.connect(self._apply_filters)
        fg.addWidget(self.ed_date_to, 1, 3)

        quick_box = QWidget()
        qb = QHBoxLayout(quick_box)
        qb.setContentsMargins(0, 0, 0, 0)
        qb.setSpacing(6)

        self.btn_today = QPushButton("Сегодня")
        self.btn_today.setObjectName("quickdate")
        self.btn_today.clicked.connect(lambda: self._set_quick_date("today"))
        qb.addWidget(self.btn_today)

        self.btn_yesterday = QPushButton("Вчера")
        self.btn_yesterday.setObjectName("quickdate")
        self.btn_yesterday.clicked.connect(
            lambda: self._set_quick_date("yesterday"))
        qb.addWidget(self.btn_yesterday)

        self.btn_month = QPushButton("За месяц")
        self.btn_month.setObjectName("quickdate")
        self.btn_month.clicked.connect(lambda: self._set_quick_date("month"))
        qb.addWidget(self.btn_month)

        fg.addWidget(quick_box, 1, 4)

        btn_clear = QPushButton("Сбросить")
        btn_clear.clicked.connect(self._clear_filters)
        fg.addWidget(btn_clear, 1, 5)

        cv.addWidget(box_filter)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            "ID", "Пациент", "Пол", "Дата рожд.", "Дата иссл.",
            "Модальность", "Описание", "StudyUID"
        ])
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            6, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            7, QHeaderView.Interactive)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSortIndicatorShown(True)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.doubleClicked.connect(self.view_selected)
        self.table.itemSelectionChanged.connect(self._on_selection)
        cv.addWidget(self.table, 1)

        hb = QHBoxLayout()
        self.btn_view = QPushButton("🔍 Просмотр снимка")
        self.btn_view.setObjectName("primary")
        self.btn_view.setMinimumHeight(40)
        self.btn_view.clicked.connect(self.view_selected)
        self.btn_view.setEnabled(False)

        self.btn_edit_pat = QPushButton("✎ Редактировать пациента")
        self.btn_edit_pat.setMinimumHeight(40)
        self.btn_edit_pat.clicked.connect(self.edit_patient)
        self.btn_edit_pat.setEnabled(False)

        self.btn_describe = QPushButton("📝 Описание")
        self.btn_describe.setMinimumHeight(40)
        self.btn_describe.clicked.connect(self.view_selected)

        hb.addWidget(self.btn_view)
        hb.addWidget(self.btn_describe)
        hb.addWidget(self.btn_edit_pat)
        hb.addStretch(1)
        cv.addLayout(hb)

        root.addWidget(center, 1)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.progress.setVisible(False)
        cv.addWidget(self.progress)

        self.statusBar().showMessage(
            "Готов · Двойной клик — просмотр · Клик по заголовку — сортировка")
        self._update_counts()

    def _set_quick_date(self, mode):
        today = QDate.currentDate()
        self.ed_date_from.blockSignals(True)
        self.ed_date_to.blockSignals(True)
        if mode == "today":
            self.ed_date_from.setDate(today)
            self.ed_date_to.setDate(today)
        elif mode == "yesterday":
            y = today.addDays(-1)
            self.ed_date_from.setDate(y)
            self.ed_date_to.setDate(y)
        elif mode == "month":
            self.ed_date_from.setDate(today.addDays(-30))
            self.ed_date_to.setDate(today)
        self.ed_date_from.blockSignals(False)
        self.ed_date_to.blockSignals(False)
        self._apply_filters()

    def _update_counts(self):
        p = self.db.conn.execute(
            "SELECT COUNT(*) FROM patients").fetchone()[0]
        s = self.db.conn.execute(
            "SELECT COUNT(*) FROM studies").fetchone()[0]
        u = self.db.count_undescribed()
        self.lbl_counts.setText(
            f"Пациентов: {p}\nИсследований: {s}\nБез описания: {u}"
        )

    def _set_filter_mode(self, mode):
        self.current_filter = mode
        self.btn_nav_all.setChecked(mode == "all")
        self.btn_nav_undescr.setChecked(mode == "undescribed")
        self.btn_nav_patients.setChecked(mode == "patients")
        self._load_studies()

    def _clear_filters(self):
        self.ed_search.blockSignals(True)
        self.ed_date_from.blockSignals(True)
        self.ed_date_to.blockSignals(True)
        self.ed_search.clear()
        self.ed_date_from.setDate(QDate(2000, 1, 1))
        self.ed_date_to.setDate(QDate.currentDate().addYears(1))
        self.ed_search.blockSignals(False)
        self.ed_date_from.blockSignals(False)
        self.ed_date_to.blockSignals(False)
        self._load_studies()

    def _apply_filters(self):
        self._load_studies()

    def _load_studies(self):
        text = self.ed_search.text().strip()
        d_from = self.ed_date_from.date().toString("yyyy-MM-dd")
        d_to = self.ed_date_to.date().toString("yyyy-MM-dd")
        only_undescr = (self.current_filter == "undescribed")

        rows = self.db.search_studies(
            text=text, date_from=d_from, date_to=d_to,
            only_undescribed=only_undescr)

        self._current_study_ids = [r["id"] for r in rows]

        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)

        for r in rows:
            row = self.table.rowCount()
            self.table.insertRow(row)

            fio = (f"{r['last_name']} {r['first_name']} "
                   f"{r['middle_name']}").strip()
            has_desc = bool(r["description"] and r["description"].strip())
            try:
                sex_val = r["p_sex"]
            except (IndexError, KeyError):
                sex_val = 0
            sex_text = "М" if sex_val else "Ж"

            pid_text = str(r["patient_id"])
            fio_text = fix_cp1251(fio)
            sex_key = sex_val
            dob_text = r["p_dob"] or ""
            date_text = f"{r['study_date'] or ''} {r['study_time'] or ''}".strip()
            modality_text = r["modality"] or ""
            desc_text = "✓ есть" if has_desc else "— нет"
            uid_text = (r["study_uid"][:32] + "…") if r["study_uid"] else ""

            pid_key = int(r["patient_id"]) if str(r["patient_id"]).isdigit() else 0
            fio_key = fio_text.upper()
            dob_key = dob_text or "0000-00-00"
            date_key = (r["study_date"] or "0000-00-00") + " " + \
                       (r["study_time"] or "00:00:00")
            modality_key = modality_text or ""
            desc_key = 0 if not has_desc else 1
            uid_key = r["study_uid"] or ""

            items = [
                SortableItem(pid_text, pid_key),
                SortableItem(fio_text, fio_key),
                SortableItem(sex_text, sex_key),
                SortableItem(dob_text, dob_key),
                SortableItem(date_text, date_key),
                SortableItem(modality_text, modality_key),
                SortableItem(desc_text, desc_key),
                SortableItem(uid_text, uid_key),
            ]

            for c, it in enumerate(items):
                if c == 6 and has_desc:
                    it.setForeground(QBrush(QColor("#06d6a0")))
                elif c == 6:
                    it.setForeground(QBrush(QColor("#ffd166")))
                if c == 2:
                    it.setTextAlignment(Qt.AlignCenter)
                if c == 0:
                    it.setData(Qt.UserRole, r["id"])
                self.table.setItem(row, c, it)

        self.table.setSortingEnabled(True)

        self.statusBar().showMessage(
            f"Показано исследований: {len(rows)}  ·  "
            f"клик по заголовку — сортировка")
        self._update_counts()

    def _on_selection(self):
        has = self.table.currentRow() >= 0
        self.btn_view.setEnabled(has)
        self.btn_edit_pat.setEnabled(has)

    def _selected_study_id(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        it = self.table.item(row, 0)
        return it.data(Qt.UserRole) if it else None

    def _selected_patient_id(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        it = self.table.item(row, 0)
        if not it:
            return None
        try:
            return int(it.text())
        except Exception:
            return None

    def view_selected(self):
        sid = self._selected_study_id()
        if not sid:
            return
        study_ids = list(self._current_study_ids)
        if sid not in study_ids:
            study_ids = [sid]
        try:
            idx = study_ids.index(sid)
        except ValueError:
            idx = 0

        try:
            self.viewer = ViewerWindow(self.db, study_ids, idx, self)
            self.viewer.showMaximized()
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", str(e))

    def edit_patient(self):
        pid = self._selected_patient_id()
        if not pid:
            return
        dlg = PatientDialog(self.db, pid, self)
        if dlg.exec_() == QDialog.Accepted:
            self._load_studies()

    def open_settings(self):
        dlg = SettingsDialog(self.db, self)
        if dlg.exec_() == QDialog.Accepted:
            self._load_studies()

    def import_from_disk(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Папка с DICOM-файлами на транспортном диске", "")
        if not folder:
            return
        if not HAS_PYDICOM:
            QMessageBox.critical(self, "Ошибка", "Не установлен pydicom.")
            return

        mdb_path = SETTINGS.value("db/mdb_path", "", type=str)
        storage = SETTINGS.value(
            "db/storage", str(_app_dir() / "storage"), type=str)

        ans = QMessageBox.question(
            self, "Импорт с диска",
            f"Источник: {folder}\n\n"
            f"Хранилище DICOM: {storage}\n\n"
            f"База .mdb: {mdb_path or '(не указана)'}\n\n"
            "Все найденные исследования будут добавлены в SQLite.\n"
            "DICOM-файлы будут скопированы в общее хранилище "
            "с именем по пациенту.\n"
            "Если указана .mdb — запись также попадёт в неё.\n"
            "Существующие записи будут пропущены.\n\n"
            "Продолжить?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if ans != QMessageBox.Yes:
            return

        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.statusBar().showMessage("Импорт…")

        self.import_worker = ImportWorker(
            folder, self.db.path,
            mdb_path=mdb_path if mdb_path else None,
            local_storage=storage)
        self.import_worker.progress.connect(self._on_import_progress)
        self.import_worker.status.connect(self._on_import_status)
        self.import_worker.row_added.connect(self._on_import_row)
        self.import_worker.finished_stats.connect(self._on_import_done)
        self.import_worker.start()

    def _on_import_progress(self, cur, total):
        if total <= 0:
            return
        pct = int(cur * 100 / total)
        self.progress.setValue(pct)

    def _on_import_status(self, msg):
        self.statusBar().showMessage(msg)

    def _on_import_row(self, row):
        pass

    def _on_import_done(self, stats):
        self.progress.setVisible(False)
        self._load_studies()
        text = (
            f"Всего исследований: {stats['total']}\n"
            f"Импортировано:       {stats['imported']}\n"
            f"  в т.ч. новых пациентов: {stats['new_patients']}\n"
            f"  новых исследований: {stats['new_studies']}\n"
            f"Скопировано файлов:  {stats.get('files_copied', 0)}\n"
            f"Пропущено (дубликаты): {stats['skipped']}\n"
            f"Ошибок:              {stats['errors']}\n"
        )
        if stats["errors_list"]:
            text += "\nПервый пример ошибки:\n"
            text += "\n".join(stats["errors_list"][:3])
        box = QMessageBox(self)
        box.setWindowTitle("Импорт завершён")
        box.setIcon(QMessageBox.Warning if stats["errors"]
                    else QMessageBox.Information)
        box.setText(text)
        box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.exec_()
        self.statusBar().showMessage(
            f"Импорт завершён. Новых: {stats['imported']}")

    def closeEvent(self, event):
        try:
            self.db.close()
        except Exception:
            pass
        super().closeEvent(event)


# =====================================================================
def main():
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_QSS)

    app.setApplicationName("Печать исследования")
    app.setApplicationDisplayName("Печать исследования")
    app.setOrganizationName("DICOM Viewer")

    icon_path = _icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(str(icon_path)))
        if sys.platform.startswith("win"):
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "DICOM.Viewer.1")
            except Exception:
                pass

    w = MainWindow()
    w.showMaximized()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()