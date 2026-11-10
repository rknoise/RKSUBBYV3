# -*- coding: utf-8 -*-
"""rksubby V3 – sötétkék alap, rózsaszín → lila → kék átmenetes kiemelés (Qt stíluslap)."""

BG = "#070a16"
PANEL = "#0e1226"
PANEL2 = "#141a33"
BORDER = "#222a4a"
TEXT = "#eef0ff"
MUTED = "#8a92b8"
ACCENT = "#ff3fb4"      # rózsaszín
ACCENT2 = "#2f8cff"     # kék
PURPLE = "#9b4dff"
RED = "#ff3b5c"
GREEN = "#38d27a"
YELLOW = "#ffc53d"

QSS = f"""
* {{ font-family: 'Segoe UI'; font-size: 10pt; color: {TEXT}; }}
QMainWindow, QDialog {{ background: {BG}; }}
QWidget#Root {{ background: {BG}; }}
QToolTip {{ background: {PANEL2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px 6px; }}

QFrame#Header {{ background: transparent; }}
QLabel#Chip {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 11px; padding: 3px 10px;
               color: {MUTED}; font-size: 9pt; }}
QLabel#Chip[state="ok"] {{ color: {TEXT}; border-color: #2c3f70; }}
QLabel#Chip[state="warn"] {{ color: {YELLOW}; border-color: #5a4a20; }}
QLabel#Chip[state="err"] {{ color: {RED}; border-color: #5a2630; }}
QLabel#Chip[state="rec"] {{ color: white; background: #3a1030; border-color: {ACCENT}; }}

QToolButton#SrcBtn {{ background: {PANEL2}; border: 1px solid #3a2f6a; border-radius: 11px; padding: 3px 8px;
                      color: {TEXT}; font-size: 9pt; }}
QToolButton#SrcBtn:hover {{ border-color: {PURPLE}; }}
QToolButton#SrcBtn::menu-indicator {{ subcontrol-position: right center; right: 6px; }}
QPushButton#UpdateBtn {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {ACCENT}, stop:1 {PURPLE});
                         border: none; border-radius: 11px; padding: 4px 12px; color: white; font-size: 9pt; }}
QMenu {{ background: {PANEL2}; border: 1px solid {BORDER}; padding: 6px; }}
QMenu::item {{ padding: 6px 22px 6px 10px; border-radius: 6px; }}
QMenu::item:selected {{ background: #241d4d; }}
QMenu::section {{ color: {MUTED}; padding: 8px 10px 4px 10px; font-size: 8.5pt; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 6px; }}
QFrame#VideoFrame {{ background: #04060d; border: 1px solid {BORDER}; border-radius: 14px; }}
QFrame#ControlBar, QFrame#Card {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 14px; }}
QFrame#Side {{ background: transparent; }}
QLabel#CardTitle {{ font-size: 10.5pt; font-weight: 600; color: {TEXT}; }}
QLabel#Muted, QLabel#Small {{ color: {MUTED}; }}
QLabel#Small {{ font-size: 8.5pt; }}
QLabel#Timer {{ font-family: 'Segoe UI Semibold'; font-size: 15pt; color: {TEXT}; }}
QLabel#Size {{ color: {MUTED}; font-size: 10pt; }}
QFrame#Sep {{ background: {BORDER}; max-width: 1px; min-width: 1px; }}

QPushButton {{ background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 10px; padding: 7px 14px; }}
QPushButton:hover {{ border-color: #3a4370; background: #1a2140; }}
QPushButton:pressed {{ background: #10142a; }}
QPushButton:disabled {{ color: #4e5578; border-color: #1a2038; }}
QPushButton#Primary {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {ACCENT}, stop:0.5 {PURPLE}, stop:1 {ACCENT2});
                       border: none; color: white; font-weight: 600; }}
QPushButton#Primary:hover {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #ff62c3, stop:0.5 #ad6bff, stop:1 #55a2ff); }}
QPushButton#RecBtn {{ background: #1a1030; border: 1px solid #4a2557; color: white; font-weight: 600;
                      padding: 8px 18px; border-radius: 12px; }}
QPushButton#RecBtn:hover {{ border-color: {RED}; }}
QPushButton#RecBtn:checked {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {RED}, stop:1 {ACCENT}); border-color: {ACCENT}; }}
QPushButton#Toggle:checked {{ background: #1d1745; border-color: {PURPLE}; color: #e3d4ff; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 9px; padding: 5px; }}
QToolButton:hover {{ background: {PANEL2}; border-color: {BORDER}; }}
QToolButton:checked {{ background: #1d1745; border-color: {PURPLE}; }}

QSlider::groove:horizontal {{ height: 4px; background: #252c4d; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {ACCENT}, stop:1 {PURPLE}); border-radius: 2px; }}
QSlider::handle:horizontal {{ background: white; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}

QProgressBar {{ background: #1a2040; border: none; border-radius: 4px; height: 8px; text-align: center;
                font-size: 1px; color: transparent; }}
QProgressBar::chunk {{ border-radius: 4px; background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                       stop:0 {ACCENT}, stop:0.5 {PURPLE}, stop:1 {ACCENT2}); }}
QProgressBar#Level::chunk {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #2bd47d,
                             stop:0.75 {YELLOW}, stop:1 {RED}); }}

QListWidget {{ background: transparent; border: none; outline: none; }}
QListWidget::item {{ border-bottom: 1px solid #1a2038; padding: 6px 2px; }}
QListWidget::item:selected {{ background: #1d1745; border-radius: 8px; color: {TEXT}; }}
QListWidget#Nav::item {{ border: none; padding: 9px 12px; border-radius: 9px; margin: 1px 0; }}
QListWidget#Nav::item:selected {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #3a1640, stop:1 #1d1745); color: white; }}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background: #0a0e1e; border: 1px solid {BORDER};
    border-radius: 8px; padding: 6px 8px; selection-background-color: {ACCENT}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: {PANEL2}; border: 1px solid {BORDER}; selection-background-color: #2a2060; }}
QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px; }}
QCheckBox::indicator {{ border: 1px solid #3a4370; border-radius: 4px; background: #0a0e1e; }}
QCheckBox::indicator:checked {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 {ACCENT}, stop:1 {PURPLE}); border-color: {PURPLE}; }}
QRadioButton::indicator {{ border: 1px solid #3a4370; border-radius: 8px; background: #0a0e1e; }}
QRadioButton::indicator:checked {{ background: qradialgradient(cx:0.5,cy:0.5,radius:0.5,fx:0.5,fy:0.5,
                                   stop:0 {ACCENT}, stop:0.55 {ACCENT}, stop:0.6 #0a0e1e); border-color: {ACCENT}; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 12px; margin-top: 14px; padding: 12px 10px 10px 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {MUTED}; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #2a3258; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QStatusBar {{ background: {BG}; color: {MUTED}; border-top: 1px solid #141a30; }}
QStatusBar QLabel {{ color: {MUTED}; padding: 0 6px; }}
QMessageBox {{ background: {PANEL}; }}
"""
