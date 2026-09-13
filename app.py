"""
Text-to-Audio — озвучка текста через Google Cloud TTS (с таймингом или без)
============================================================================
Поддерживает форматы тайминга:
  - (00:52–01:22) инлайн или на отдельной строке
  - SRT субтитры: 00:00:01,000 --> 00:00:04,000
  - [00:01:30] Текст
  - 00:01:30 - Текст  /  00:01:30 — Текст
  - Просто текст без тайминга

Режимы:
  simple — убрать тайминг, озвучить весь текст (автосплит по 4800 байт)
  timed  — соблюдать паузы между сегментами (строит WAV timeline)

Запуск:  python app.py
         Открыть  http://localhost:8767
"""

import io
import logging
import re
import struct
import subprocess
import unicodedata
import sys
import tempfile
import uuid
import wave
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from google.cloud import texttospeech
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
_AI = ROOT
if str(_AI) not in sys.path:
    sys.path.insert(0, str(_AI))
from env_loader import load_env_stack

load_env_stack(ROOT)

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
OUTPUT_DIR = ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

SAMPLE_RATE = 24_000   # 24 kHz — Google TTS LINEAR16
BYTES_PER_SAMPLE = 2   # 16-bit

# BCP-47 коды языков (50 языков)
LANG_CODES = {
    "es": "es-ES", "en": "en-US", "hi": "hi-IN", "ar": "ar-XA",
    "pt": "pt-BR", "ru": "ru-RU", "zh": "cmn-CN", "ja": "ja-JP",
    "de": "de-DE", "bn": "bn-IN", "fr": "fr-FR", "fil": "fil-PH",
    "vi": "vi-VN", "tr": "tr-TR", "fa": "fa-IR", "id": "id-ID",
    "ko": "ko-KR", "it": "it-IT", "th": "th-TH", "pa": "pa-IN",
    "te": "te-IN", "ms": "ms-MY", "ta": "ta-IN", "mr": "mr-IN",
    "nl": "nl-NL", "pl": "pl-PL", "uk": "uk-UA", "yue": "yue-HK",
    "gu": "gu-IN", "uz": "uz-UZ", "sw": "sw-KE", "ro": "ro-RO",
    "cs": "cs-CZ", "yo": "yo-NG", "hu": "hu-HU", "sv": "sv-SE",
    "am": "am-ET", "el": "el-GR", "bg": "bg-BG", "sr": "sr-RS",
    "he": "he-IL", "kk": "kk-KZ", "af": "af-ZA", "hr": "hr-HR",
    "fi": "fi-FI", "da": "da-DK", "nb": "nb-NO", "sk": "sk-SK",
    "lt": "lt-LT", "sl": "sl-SI",
}

# Голоса для основных языков (Chirp3-HD + Wavenet/Standard резерв)
VOICES: dict[str, list[dict]] = {
    "ru-RU": [
        {"name": "ru-RU-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "ru-RU-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "ru-RU-Chirp3-HD-Kore",    "label": "Женский — Kore (HD)",    "gender": "female"},
        {"name": "ru-RU-Chirp3-HD-Leda",    "label": "Женский — Leda (HD)",    "gender": "female"},
        {"name": "ru-RU-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "ru-RU-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "en-US": [
        {"name": "en-US-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "en-US-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "en-US-Chirp3-HD-Kore",    "label": "Женский — Kore (HD)",    "gender": "female"},
        {"name": "en-US-Chirp3-HD-Leda",    "label": "Женский — Leda (HD)",    "gender": "female"},
        {"name": "en-US-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "en-US-Wavenet-C",          "label": "Женский — Wavenet C",   "gender": "female"},
    ],
    "fr-FR": [
        {"name": "fr-FR-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "fr-FR-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "fr-FR-Chirp3-HD-Kore",    "label": "Женский — Kore (HD)",    "gender": "female"},
        {"name": "fr-FR-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "fr-FR-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "de-DE": [
        {"name": "de-DE-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "de-DE-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "de-DE-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "de-DE-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "es-ES": [
        {"name": "es-ES-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "es-ES-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "es-ES-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "es-US-Wavenet-A",          "label": "Женский — Wavenet A (US)", "gender": "female"},
    ],
    "pt-BR": [
        {"name": "pt-BR-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "pt-BR-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "pt-BR-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "pt-BR-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "uk-UA": [
        {"name": "uk-UA-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "uk-UA-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "uk-UA-Standard-A",         "label": "Женский — Standard A",  "gender": "female"},
    ],
    "it-IT": [
        {"name": "it-IT-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "it-IT-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "it-IT-Wavenet-C",          "label": "Мужской — Wavenet C",   "gender": "male"},
        {"name": "it-IT-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "nl-NL": [
        {"name": "nl-NL-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "nl-NL-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "nl-NL-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "nl-NL-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "pl-PL": [
        {"name": "pl-PL-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "pl-PL-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "pl-PL-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "pl-PL-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "tr-TR": [
        {"name": "tr-TR-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "tr-TR-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "tr-TR-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "tr-TR-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "ja-JP": [
        {"name": "ja-JP-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "ja-JP-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "ja-JP-Wavenet-C",          "label": "Мужской — Wavenet C",   "gender": "male"},
        {"name": "ja-JP-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "ko-KR": [
        {"name": "ko-KR-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "ko-KR-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "ko-KR-Wavenet-C",          "label": "Мужской — Wavenet C",   "gender": "male"},
        {"name": "ko-KR-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "cmn-CN": [
        {"name": "cmn-CN-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "cmn-CN-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "cmn-CN-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "cmn-CN-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "ar-XA": [
        {"name": "ar-XA-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "ar-XA-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "ar-XA-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "ar-XA-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "hi-IN": [
        {"name": "hi-IN-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "hi-IN-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "hi-IN-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "hi-IN-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "id-ID": [
        {"name": "id-ID-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "id-ID-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "id-ID-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "id-ID-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "vi-VN": [
        {"name": "vi-VN-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "vi-VN-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "vi-VN-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "vi-VN-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "th-TH": [
        {"name": "th-TH-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "th-TH-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "th-TH-Standard-A",         "label": "Женский — Standard A",  "gender": "female"},
    ],
    "sv-SE": [
        {"name": "sv-SE-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "sv-SE-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "sv-SE-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "sv-SE-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "el-GR": [
        {"name": "el-GR-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "el-GR-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "el-GR-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
    ],
    "fi-FI": [
        {"name": "fi-FI-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "fi-FI-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "fi-FI-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "da-DK": [
        {"name": "da-DK-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "da-DK-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "da-DK-Wavenet-C",          "label": "Мужской — Wavenet C",   "gender": "male"},
        {"name": "da-DK-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "nb-NO": [
        {"name": "nb-NO-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "nb-NO-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "nb-NO-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "nb-NO-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "cs-CZ": [
        {"name": "cs-CZ-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "cs-CZ-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "cs-CZ-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "ro-RO": [
        {"name": "ro-RO-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "ro-RO-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "ro-RO-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "hu-HU": [
        {"name": "hu-HU-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "hu-HU-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "hu-HU-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "bg-BG": [
        {"name": "bg-BG-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "bg-BG-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "bg-BG-Standard-A",         "label": "Женский — Standard A",  "gender": "female"},
    ],
    "sk-SK": [
        {"name": "sk-SK-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "sk-SK-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "sk-SK-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "he-IL": [
        {"name": "he-IL-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "he-IL-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "he-IL-Wavenet-C",          "label": "Мужской — Wavenet C",   "gender": "male"},
        {"name": "he-IL-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "af-ZA": [
        {"name": "af-ZA-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "af-ZA-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "af-ZA-Standard-A",         "label": "Женский — Standard A",  "gender": "female"},
    ],
    "ms-MY": [
        {"name": "ms-MY-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "ms-MY-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "ms-MY-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "ms-MY-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "ta-IN": [
        {"name": "ta-IN-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "ta-IN-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "ta-IN-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "ta-IN-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "te-IN": [
        {"name": "te-IN-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "te-IN-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "te-IN-Standard-B",         "label": "Мужской — Standard B",  "gender": "male"},
        {"name": "te-IN-Standard-A",         "label": "Женский — Standard A",  "gender": "female"},
    ],
    "bn-IN": [
        {"name": "bn-IN-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "bn-IN-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "bn-IN-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "bn-IN-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "gu-IN": [
        {"name": "gu-IN-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "gu-IN-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "gu-IN-Standard-B",         "label": "Мужской — Standard B",  "gender": "male"},
        {"name": "gu-IN-Standard-A",         "label": "Женский — Standard A",  "gender": "female"},
    ],
    "mr-IN": [
        {"name": "mr-IN-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "mr-IN-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "mr-IN-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "mr-IN-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
    "sw-KE": [
        {"name": "sw-KE-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "sw-KE-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "sw-TZ-Standard-A",         "label": "Мужской — Standard A",  "gender": "male"},
    ],
    "yue-HK": [
        {"name": "yue-HK-Standard-B",        "label": "Мужской — Standard B",  "gender": "male"},
        {"name": "yue-HK-Standard-A",        "label": "Женский — Standard A",  "gender": "female"},
        {"name": "yue-HK-Standard-C",        "label": "Мужской — Standard C",  "gender": "male"},
        {"name": "yue-HK-Standard-D",        "label": "Женский — Standard D",  "gender": "female"},
    ],
    "fil-PH": [
        {"name": "fil-PH-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
        {"name": "fil-PH-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
        {"name": "fil-PH-Wavenet-B",          "label": "Мужской — Wavenet B",   "gender": "male"},
        {"name": "fil-PH-Wavenet-A",          "label": "Женский — Wavenet A",   "gender": "female"},
    ],
}

# Для языков без явного списка — Chirp3-HD + Standard резерв
_DEFAULT_VOICES = [
    {"name": "{lang}-Chirp3-HD-Algenib", "label": "Мужской — Algenib (HD)", "gender": "male"},
    {"name": "{lang}-Chirp3-HD-Aoede",   "label": "Женский — Aoede (HD)",   "gender": "female"},
    {"name": "{lang}-Standard-A",        "label": "Женский — Standard A",   "gender": "female"},
]


def get_voices_for_lang(lang_code: str) -> list[dict]:
    if lang_code in VOICES:
        return VOICES[lang_code]
    return [
        {**v, "name": v["name"].replace("{lang}", lang_code)}
        for v in _DEFAULT_VOICES
    ]


# ---------------------------------------------------------------------------
# Парсинг тайминга
# ---------------------------------------------------------------------------

# SRT: "00:00:01,000 --> 00:00:04,000"
_RE_SRT_TC   = re.compile(r"^\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}")
# Просто число (индекс SRT)
_RE_SRT_IDX  = re.compile(r"^\d+\s*$")
# WebVTT заголовок
_RE_VTT_HDR  = re.compile(r"^WEBVTT", re.IGNORECASE)
# WebVTT NOTE
_RE_VTT_NOTE = re.compile(r"^NOTE\b", re.IGNORECASE)
# [00:01:30] или [1:30] в начале строки
_RE_BRACKET  = re.compile(r"^\[(\d{1,2}:)?(\d{1,2}:\d{2})\]")
# 00:01:30 - текст  /  00:01:30 — текст  /  01:30 - текст
_RE_LEADING  = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?\s*[-—]\s*")
# (00:52–01:22) или (00:52-01:22) — диапазон в скобках, ВЕЗДЕ в тексте
# group(1) = start, group(2) = end (end не используется, только start)
_RE_PAREN_INLINE = re.compile(
    r"\((\d{1,2}:\d{2}(?::\d{2})?)\s*[–\-]\s*(\d{1,2}:\d{2}(?::\d{2})?)\)"
)
# Нестандартная запись мин:сек через «h»: (00h01-00h08) — в DOCX/таблицах бывает чаще первого блока
_RE_H_SEP_CLOCK = re.compile(r"(?<!\d)(\d{1,2})[hH](\d{2})(?!\d)")


def normalize_clock_h_separator(text: str) -> str:
    """00h01 → 00:01, чтобы парсер и SRT-логика видели обычный таймкод."""
    return _RE_H_SEP_CLOCK.sub(r"\1:\2", text)


def _tc_to_seconds(tc: str) -> float:
    """'HH:MM:SS,mmm' или 'MM:SS' -> секунды"""
    tc = tc.replace(",", ".").replace(";", ".")
    parts = tc.split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    if len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    return float(parts[0])


def strip_timing(text: str) -> str:
    """Убрать все тайминги — вернуть чистый текст для озвучки."""
    text = normalize_clock_h_separator(text)
    # Сначала убрать все инлайн-тайминги (00:52-01:22) в любом месте текста
    text = _RE_PAREN_INLINE.sub("", text)

    lines = text.splitlines()
    clean: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if _RE_SRT_TC.match(line):
            continue
        if _RE_SRT_IDX.match(line):
            continue
        if _RE_VTT_HDR.match(line):
            continue
        # Убрать [timestamp] или timestamp - в начале
        if _RE_BRACKET.match(line):
            line = _RE_BRACKET.sub("", line).strip()
        elif _RE_LEADING.match(line):
            line = _RE_LEADING.sub("", line).strip()
        if line:
            clean.append(line)
    return " ".join(clean)


def _prefix_before_first_srt_line(lines: list[str]) -> str | None:
    """Произвольный текст до первой строки SRT (`00:00:00,000 --> ...`)."""
    first = None
    for i, raw in enumerate(lines):
        if _RE_SRT_TC.match(raw.strip()):
            first = i
            break
    if first is None or first == 0:
        return None
    parts: list[str] = []
    for j in range(first):
        s = lines[j].strip()
        if not s:
            continue
        if _RE_SRT_IDX.match(s):
            continue
        if _RE_VTT_HDR.match(s):
            continue
        if _RE_VTT_NOTE.match(s):
            continue
        parts.append(s)
    if not parts:
        return None
    cleaned = strip_timing(" ".join(parts)).strip()
    return cleaned or None


def _prefix_before_first_bracket_or_leading_line(lines: list[str]) -> str | None:
    """Произвольный текст до первой строки с [MM:SS] или «MM:SS - текст»."""
    first = None
    for i, raw in enumerate(lines):
        s = raw.strip()
        if not s:
            continue
        if _RE_BRACKET.match(s) or _RE_LEADING.match(s):
            first = i
            break
    if first is None or first == 0:
        return None
    parts = [lines[j].strip() for j in range(first) if lines[j].strip()]
    if not parts:
        return None
    cleaned = strip_timing(" ".join(parts)).strip()
    return cleaned or None


def parse_timed_segments(text: str) -> list[tuple[float, str]]:
    """
    Вернуть список (start_seconds, text_фрагмент).
    Если тайминг не найден — вернуть один сегмент с offset=0.
    """
    text = normalize_clock_h_separator(text)
    segments: list[tuple[float, str]] = []
    lines = text.splitlines()

    srt_head_prefix = _prefix_before_first_srt_line(lines)

    # --- SRT / WebVTT ---
    i = 0
    srt_found = False
    while i < len(lines):
        line = lines[i].strip()
        if _RE_SRT_TC.match(line):
            # Извлечь start time
            start_str = line.split("-->")[0].strip()
            start_sec = _tc_to_seconds(start_str)
            # Собрать текст до пустой строки
            i += 1
            txt_lines = []
            while i < len(lines) and lines[i].strip():
                seg_line = lines[i].strip()
                # Удалить HTML теги типа <i>, <b>
                seg_line = re.sub(r"<[^>]+>", "", seg_line)
                if seg_line:
                    txt_lines.append(seg_line)
                i += 1
            if txt_lines:
                segments.append((start_sec, " ".join(txt_lines)))
                srt_found = True
        else:
            i += 1

    if srt_found:
        if srt_head_prefix:
            segments.insert(0, (0.0, srt_head_prefix))
        return segments

    # --- (MM:SS-MM:SS) в скобках — ЛЮБОЙ формат (инлайн или построчный) ---
    # Ищем все вхождения паттерна в тексте целиком (склеенном в одну строку).
    # Это работает и для инлайн: "(00:02-00:29) Текст (00:30-00:47) Текст"
    # и для построчного:
    #   (00:52–01:22)
    #   Текст строка 1
    #   Текст строка 2
    flat_text = " ".join(lines)
    inline_matches = list(_RE_PAREN_INLINE.finditer(flat_text))
    if inline_matches:
        # Текст ДО первого таймкода раньше отбрасывался — в треке получалась «немая»
        # минута или потерянное вступление. Озвучиваем префикс с offset 0.
        raw_pre = flat_text[: inline_matches[0].start()].strip()
        prefix = strip_timing(raw_pre).strip() if raw_pre else ""
        if prefix:
            segments.append((0.0, prefix))
        for idx, m in enumerate(inline_matches):
            start_sec = _tc_to_seconds(m.group(1))
            seg_start = m.end()
            seg_end = inline_matches[idx + 1].start() if idx + 1 < len(inline_matches) else len(flat_text)
            seg_text = _RE_PAREN_INLINE.sub("", flat_text[seg_start:seg_end]).strip()
            if seg_text:
                segments.append((start_sec, seg_text))
        if segments:
            return segments

    # --- [timestamp] или timestamp - текст ---
    for line in lines:
        line = line.strip()
        if not line:
            continue
        m_bracket = _RE_BRACKET.match(line)
        m_leading = _RE_LEADING.match(line)
        if m_bracket:
            ts_str = m_bracket.group(0).strip("[]").strip()
            start_sec = _tc_to_seconds(ts_str)
            txt = _RE_BRACKET.sub("", line).strip()
            if txt:
                segments.append((start_sec, txt))
        elif m_leading:
            ts_str = m_leading.group(0).split("-")[0].split("—")[0].strip()
            start_sec = _tc_to_seconds(ts_str)
            txt = _RE_LEADING.sub("", line).strip()
            if txt:
                segments.append((start_sec, txt))

    if segments:
        bl_prefix = _prefix_before_first_bracket_or_leading_line(lines)
        if bl_prefix:
            segments.insert(0, (0.0, bl_prefix))
        return segments

    # --- Нет тайминга — весь текст одним куском ---
    plain = strip_timing(text)
    if plain:
        return [(0.0, plain)]
    return []


# ---------------------------------------------------------------------------
# Google TTS
# ---------------------------------------------------------------------------

# UTF-8 последовательности «математических» скобок / BIG SOLIDUS — в ответе Google
# они иногда фигурируют в grpc_message; убираем из wire-строки на всякий случай.
_TTS_UTF8_DROP_SEQS = (
    "\u27ea".encode("utf-8"),
    "\u27eb".encode("utf-8"),
    "\u29f8".encode("utf-8"),
)


def _tts_scrub_wire_utf8(text: str) -> str:
    """Удалить из UTF-8 тела запрещённые кодпойнты целиком (не ломая остальную кодировку)."""
    if not text:
        return text
    b = text.encode("utf-8")
    orig = b
    for seq in _TTS_UTF8_DROP_SEQS:
        b = b.replace(seq, b"")
    if b != orig:
        return b.decode("utf-8", errors="replace").strip()
    return text


def _synthesize_segment(
    client: texttospeech.TextToSpeechClient,
    text: str,
    lang_code: str,
    voice_name: str,
    encoding: texttospeech.AudioEncoding,
) -> bytes:
    """Один вызов TTS — вернуть аудио байты."""
    text = _prepare_text_for_google_tts_request(text, lang_code, voice_name)
    text = _tts_scrub_wire_utf8(text)
    if not text:
        raise ValueError("Пустой текст для сегмента TTS после очистки")
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice_params = texttospeech.VoiceSelectionParams(
        language_code=lang_code,
        name=voice_name,
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=encoding,
        sample_rate_hertz=SAMPLE_RATE if encoding == texttospeech.AudioEncoding.LINEAR16 else None,
    )
    try:
        response = client.synthesize_speech(
            input=synthesis_input,
            voice=voice_params,
            audio_config=audio_config,
        )
    except Exception as exc:
        pl = logging.getLogger("video_pipeline")
        esc = text[:240].encode("unicode_escape", errors="backslashreplace").decode("ascii", errors="replace")
        detail = (
            f"Google TTS RPC: lang={lang_code} voice={voice_name} "
            f"len={len(text)} preview={text[:200]!r} preview_esc={esc!r} err={exc}"
        )
        if pl.handlers:
            pl.error(detail, exc_info=True)
        else:
            print(detail, file=sys.stderr, flush=True)
        raise
    return response.audio_content


def _wav_to_pcm(wav_bytes: bytes) -> bytes:
    """Извлечь сырой PCM из WAV-контейнера."""
    with wave.open(io.BytesIO(wav_bytes)) as wf:
        return wf.readframes(wf.getnframes())


def _pcm_duration(pcm: bytes) -> float:
    return len(pcm) / (SAMPLE_RATE * BYTES_PER_SAMPLE)


def _silence(seconds: float) -> bytes:
    n_bytes = int(seconds * SAMPLE_RATE) * BYTES_PER_SAMPLE
    return b"\x00" * n_bytes


def _build_wav_header(pcm_len: int) -> bytes:
    """RIFF WAV заголовок для mono 16-bit 24kHz PCM."""
    data_size  = pcm_len
    riff_size  = 36 + data_size
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", riff_size, b"WAVE",
        b"fmt ", 16, 1, 1,     # PCM, mono
        SAMPLE_RATE, SAMPLE_RATE * BYTES_PER_SAMPLE,
        BYTES_PER_SAMPLE, 16,  # block align, bits per sample
        b"data", data_size,
    )
    return header


def _pcm_to_mp3(pcm: bytes) -> bytes:
    """WAV PCM → MP3 через ffmpeg."""
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_in:
        tmp_in.write(_build_wav_header(len(pcm)))
        tmp_in.write(pcm)
        tmp_in_path = tmp_in.name

    tmp_out_path = tmp_in_path.replace(".wav", ".mp3")
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", tmp_in_path, "-codec:a", "libmp3lame",
             "-qscale:a", "2", tmp_out_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        return Path(tmp_out_path).read_bytes()
    finally:
        Path(tmp_in_path).unlink(missing_ok=True)
        Path(tmp_out_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Разбивка длинного текста на чанки (лимит Google TTS — 5000 байт)
# ---------------------------------------------------------------------------

_TTS_MAX_BYTES = 4800   # лимит Google TTS на запрос (байт UTF-8)
_TTS_MAX_SENT = 120    # безопасный лимит на одно предложение (латиница и т.п.)
_TTS_MAX_CHUNK_CHARS = 250  # меньше склейка в одном запросе — меньше «длинных» предложений для API
# Chirp / Google считает «предложение» строже для CJK: без пробелов и без ASCII-запятых
# длинные куски до «。」 дают 400 "too long".
_TTS_MAX_SENT_CJK = 28
_TTS_MAX_CHUNK_CHARS_CJK = 90
# Chirp3 (и др. Chirp): API считает «предложение» очень коротким; в ошибке видно «⟪字⧸pinyin» — это их подпись, не мусор в строке.
_CHIRP_CJK_MAX_SENT = 12
_CHIRP_CJK_MAX_CHUNK = 40
_CHIRP_CJK_MAX_HAN_RUN = 8

# Знаки конца предложения: ASCII + CJK/fullwidth + \u0964 (।) + \u06D4 (۔) + \u0589 (։) + … + ። ។
_SENT_END_CHARS = (
    ".!?\u0964\u06d4\u0589"
    "\u3002\uff01\uff1f\uff0e"  # 。！？．
    "\u2026"  # …
    "\u1362\u17d4"  # ። ។
)

# В r"..." последовательности \uXXXX не превращаются в символы — только в обычной строке.
_RE_TTS_ZWSP = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
# Матем. скобки / BIG SOLIDUS; U+27F0–U+27FF — стрелки и техсимволы около ⟪ (без блока брайля 2800–28FF).
_RE_TTS_MATH_JUNK = re.compile("[\u27e6-\u27ef\u27f0-\u27ff\u2983-\u2984\u29f5-\u29ff]")
# Явно: иногда регекс в окружении не срабатывает как ожидается — дублируем удалением.
_TTS_EXPLICIT_DROP = (
    "\u27ea\u27eb\u27ec\u27ed\u27e8\u27e9\u2983\u2984\u29f8"  # ⟪ ⟫ ⧸ ⟨ ⟩ ⫃ ⫄
)
_RE_TTS_CJK_STRONG_BREAK = re.compile("(?<=[\u3002\uff01\uff1f\uff0e\u2026\u1362\u17d4])")
_RE_TTS_LATIN_END_BREAK = re.compile("(?<=[.!?\u0964\u06d4\u0589])\\s+")
_RE_TTS_PAUSE_BREAK = re.compile("(?<=[,\uff0c\u3001\uff1b\uff1a])")
_RE_TTS_PAUSE_BREAK_DENSE = re.compile("(?<=[,\uff0c\u3001\uff1b\uff1a\u30fb])")
_RE_TTS_COMMA_ASCII_SP = re.compile(r"(?<=,)\s+")
_RE_TTS_WS = re.compile(r"\s+")

# Подстрочные глоссы перевода (⟪…⧸en), иногда после NFKC остаются варианты скобок / слэша.
_RE_TTS_GLOSS_TAIL = re.compile(
    "\u27ea"  # ⟪
    "[^\u27eb]{0,120}?"  # до закрывающей ⟫
    "(?:\u29f8|\u2044|/)"  # ⧸ или слэш
    "[a-zA-Z]{0,12}",  # короткий латинский хвост (языковой код)
    re.DOTALL,
)


def _is_unicode_private_use(cp: int) -> bool:
    return bool(
        0xE000 <= cp <= 0xF8FF
        or 0xF0000 <= cp <= 0xFFFFD
        or 0x100000 <= cp <= 0x10FFFD
    )


def _strip_chars_for_google_tts_safety(text: str) -> str:
    """
    Убрать символы, которые часто дают 400 / «длинное предложение»: матем. скобки,
    PUA-глифы из PDF, ⧼⧽ и т.п. (имена из unicodedata).
    """
    out: list[str] = []
    for c in text:
        cp = ord(c)
        if _is_unicode_private_use(cp):
            continue
        if 0x27E0 <= cp <= 0x27FF or 0x2980 <= cp <= 0x29FF:
            # Весь блок дополнительных скобок/операторов около ⟪⧸ (⟨ ⟩ уже здесь).
            continue
        try:
            nm = unicodedata.name(c)
        except ValueError:
            out.append(c)
            continue
        if nm.startswith("MATHEMATICAL "):
            continue
        if "BIG SOLIDUS" in nm:
            continue
        if cp in (0x29FC, 0x29FD):  # ⧼ ⧽ LEFT/RIGHT-POINTING CURVED ANGLE BRACKET
            continue
        if cp in (0x2329, 0x232A):  # 〈 〉 LEFT/RIGHT-POINTING ANGLE BRACKET (подстрочные глоссы)
            continue
        out.append(c)
    return "".join(out)


# Скобки, которые оставляем в cmn/yue/ja/ko; остальные Ps/Pe (в т.ч. ⟪ ⟫) — в мусор для Google.
_TTS_OK_PS_PE = frozenset("()[]{}（）【】《》「」『』〈〉［］｛｝‹›«»")


def _strip_unsafe_symbol_categories(text: str, _lang_code: str | None = None) -> str:
    """
    Убрать Sm/Sk и Ps/Pe вне белого списка для **любого** языка.
    Иначе при lang_code вроде «cmn» без «-CN» ветка «плотного» языка не срабатывала,
    ⟪ (Ps) и ⧸ (Sm) доходили до Google → 400.
    """
    out: list[str] = []
    for c in text:
        cat = unicodedata.category(c)
        if cat in ("Sm", "Sk"):
            continue
        if cat in ("Ps", "Pe") and c not in _TTS_OK_PS_PE:
            continue
        out.append(c)
    return "".join(out)


def _obliterate_interlinear_gloss(text: str) -> str:
    """
    Убрать подстрочные глоссы ⟪…⟫ / ⟪…⧸lat — до RPC Google, иначе 400 «sentence too long»
    (модель цепляет мусор в начало «предложения»).
    """
    if not text:
        return text
    prev = None
    while prev != text:
        prev = text
        text = re.sub("\u27ea[\\s\\S]*?\u27eb", "", text)
        text = re.sub(
            "\u27ea[\\s\\S]{0,240}?(?:\u29f8|\u2044|\u2215|\uff0f|/)[a-zA-Z]{0,24}",
            "",
            text,
        )
        for ch in ("\u27ea", "\u27eb", "\u29f8", "\u27e8", "\u27e9", "\u2983", "\u2984"):
            text = text.replace(ch, "")
    return text


def _normalize_ascii_comma_for_dense(text: str, lang_code: str | None) -> str:
    """Для zh/cmn/… ASCII-запятая не режет предложение для Google — в полноширинную."""
    if not text or not _lang_uses_dense_script(lang_code):
        return text
    return text.replace(",", "\uff0c")


def _strip_gloss_latin_tail_between_cjk(text: str, lang_code: str | None) -> str:
    """Остаток латиницы от «shi/jie» между двумя CJK: одна буква s."""
    if not text or not _lang_uses_dense_script(lang_code):
        return text
    return re.sub(
        r"(?<=[\u4e00-\u9fff\u3400-\u4dbf])s(?=[\u4e00-\u9fff\u3400-\u4dbf])",
        "",
        text,
    )


def _sanitize_for_tts(text: str, lang_code: str | None = None) -> str:
    """Убрать невидимые и «битые» символы из DOCX/PDF — иначе Google TTS даёт 400 про длинные предложения."""
    if not text:
        return text
    t = unicodedata.normalize("NFKC", text)
    t = unicodedata.normalize("NFC", t)
    t = _obliterate_interlinear_gloss(t)
    t = _RE_TTS_ZWSP.sub("", t)
    # Матем. скобки (⟪ ⟫ ⟨ ⟩), BIG SOLIDUS ⧸ — часто мусор из PDF/перевода.
    t = _RE_TTS_MATH_JUNK.sub("", t)
    for ch in _TTS_EXPLICIT_DROP:
        t = t.replace(ch, "")
    t = _RE_TTS_GLOSS_TAIL.sub("", t)
    t = _strip_chars_for_google_tts_safety(t)
    t = _strip_unsafe_symbol_categories(t, lang_code)
    # На случай если что-то прошло мимо категорий Unicode в окружении клиента
    for _ch in ("\u27ea", "\u27eb", "\u29f8", "\u27e8", "\u27e9"):
        t = t.replace(_ch, "")
    t = _obliterate_interlinear_gloss(t)
    t = _normalize_ascii_comma_for_dense(t, lang_code)
    t = _strip_gloss_latin_tail_between_cjk(t, lang_code)
    t = re.sub(r" +", " ", t)
    return t.strip()


def _is_chirp_voice(voice_name: str | None) -> bool:
    return bool(voice_name and "chirp" in voice_name.lower())


def _lang_uses_dense_script(lang_code: str | None) -> bool:
    """Китайский/японский/корейский: длинные фразы без пробелов — отдельные лимиты нарезки."""
    if not lang_code:
        return False
    lc = lang_code.lower().replace("_", "-")
    # cmn / cmn-CN; zh / zh-CN / zh-Hans (DeepL и др.); не ловим «java» по префиксу ja.
    if lc in ("zh", "zh-cn", "zh-tw", "zh-hans", "zh-hant"):
        return True
    if lc.startswith("cmn") or lc.startswith("yue"):
        return True
    if lc == "ja" or lc.startswith("ja-"):
        return True
    if lc == "ko" or lc.startswith("ko-"):
        return True
    return False


def _is_cjk_unified_char(c: str) -> bool:
    if not c:
        return False
    cp = ord(c)
    return (0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF)


def _obliterate_residual_math_by_name(text: str) -> str:
    """Удалить всё с именем MATHEMATICAL… / BIG SOLIDUS (не трогая «《》» — там нет MATHEMATICAL)."""
    out: list[str] = []
    for c in text:
        try:
            nm = unicodedata.name(c)
        except ValueError:
            out.append(c)
            continue
        if nm.startswith("MATHEMATICAL "):
            continue
        if "BIG SOLIDUS" in nm:
            continue
        out.append(c)
    return "".join(out)


def _inject_han_run_fences(
    text: str,
    lang_code: str | None,
    max_han: int = 22,
    voice_name: str | None = None,
) -> str:
    """
    Принудительно вставить 。 после длинной цепочки иероглифов без конца предложения.
    Chirp иначе считает весь блок одним «предложением» → 400.
    """
    if not text or not _lang_uses_dense_script(lang_code):
        return text
    eff = min(max_han, _CHIRP_CJK_MAX_HAN_RUN) if _is_chirp_voice(voice_name) else max_han
    out: list[str] = []
    han = 0
    for c in text:
        out.append(c)
        if c in "\u3002\uff01\uff1f\uff0e\u2026\n\r":
            han = 0
        elif _is_cjk_unified_char(c):
            han += 1
            if han >= eff:
                out.append("\u3002")
                han = 0
        else:
            han = 0
    return "".join(out)


def _merge_spurious_cjk_period(text: str, lang_code: str | None) -> str:
    """
    Убрать ложные 。 после инъекции границ: «…性。的疾病» → «…性的疾病».
    Иначе Chirp склеивает куски в одно длинное «предложение».
    """
    if not text or not _lang_uses_dense_script(lang_code):
        return text
    t = re.sub(
        r"([\u4e00-\u9fff\u3400-\u4dbf])\u3002\u7684(?=[\u4e00-\u9fff\u3400-\u4dbf])",
        r"\1的",
        text,
    )
    t = re.sub(
        r"([\u4e00-\u9fff\u3400-\u4dbf])\u3002\u662f(?=[\u4e00-\u9fff\u3400-\u4dbf])",
        r"\1是",
        t,
    )
    t = re.sub(
        r"([\u4e00-\u9fff\u3400-\u4dbf])\u3002\u5728(?=[\u4e00-\u9fff\u3400-\u4dbf])",
        r"\1在",
        t,
    )
    return re.sub("\u3002{2,}", "\u3002", t)


def _prepare_text_for_google_tts_request(
    text: str, lang_code: str, voice_name: str | None = None
) -> str:
    """
    Единая точка входа для текста в synthesize_speech: санитизация + имя Unicode + лимит длины по Хань.
    Вызывается даже если _chunk_text уже чанковал — API всё равно режет по своим правилам.
    """
    t = _sanitize_for_tts(text, lang_code)
    t = _obliterate_residual_math_by_name(t)
    t = _obliterate_interlinear_gloss(t)
    t = _inject_han_run_fences(t, lang_code, max_han=22, voice_name=voice_name)
    t = re.sub(r" +", " ", t).strip()
    t = _merge_spurious_cjk_period(t, lang_code)
    t = _obliterate_interlinear_gloss(t)
    t = _strip_gloss_latin_tail_between_cjk(t, lang_code)
    for _bad in ("\u27ea", "\u27eb", "\u29f8"):
        if _bad in t:
            t = t.replace(_bad, "")
    return t


def _tts_sentence_char_limit(lang_code: str | None, voice_name: str | None = None) -> int:
    if _lang_uses_dense_script(lang_code) and _is_chirp_voice(voice_name):
        return _CHIRP_CJK_MAX_SENT
    return _TTS_MAX_SENT_CJK if _lang_uses_dense_script(lang_code) else _TTS_MAX_SENT


def _tts_chunk_char_limit(lang_code: str | None, voice_name: str | None = None) -> int:
    if _lang_uses_dense_script(lang_code) and _is_chirp_voice(voice_name):
        return _CHIRP_CJK_MAX_CHUNK
    return _TTS_MAX_CHUNK_CHARS_CJK if _lang_uses_dense_script(lang_code) else _TTS_MAX_CHUNK_CHARS


def _break_dense_script_runs(text: str, limit: int) -> list[str]:
    """
    Разбить длинный фрагмент без достаточных точек: по китайской/японской запятой、顿号 и т.д.,
    затем грубо по limit символов. Пробел после ， не требуется (в отличие от ASCII ,).
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts = _RE_TTS_PAUSE_BREAK_DENSE.split(text)
    merged: list[str] = []
    buf = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if not buf:
            buf = p
        elif len(buf) + len(p) <= limit:
            buf += p
        else:
            merged.append(buf)
            buf = p
    if buf:
        merged.append(buf)
    out: list[str] = []
    for m in merged:
        if len(m) <= limit:
            out.append(m)
        else:
            for i in range(0, len(m), limit):
                out.append(m[i : i + limit])
    return out or [text]


def _split_sentences_for_tts(
    text: str, lang_code: str | None = None, voice_name: str | None = None
) -> list[str]:
    """Разбить по концам предложений; для CJK пробел после 。 не обязателен."""
    text = text.strip()
    if not text:
        return []
    out: list[str] = []
    cjk_parts = _RE_TTS_CJK_STRONG_BREAK.split(text)
    for cp in cjk_parts:
        cp = cp.strip()
        if not cp:
            continue
        sub = _RE_TTS_LATIN_END_BREAK.split(cp)
        for s in sub:
            s = s.strip()
            if s:
                out.append(s)
    lim = _tts_sentence_char_limit(lang_code, voice_name)
    if _lang_uses_dense_script(lang_code):
        out2: list[str] = []
        for s in out:
            out2.extend(_break_dense_script_runs(s, lim))
        out = out2
    return out


def _ends_with_punct(s: str) -> bool:
    return bool(s) and s[-1] in _SENT_END_CHARS


# Конец «предложения» для API (латиница).
_STRONG_SENT_END = frozenset(".!?\u3002\uff01\uff1f\uff0e\u2026")


def _has_cjk_sentence_terminal(s: str) -> bool:
    """Для cmn/ja/ko Google разбивает по 。！？ а не по ASCII . — иначе один «длинный» сегмент."""
    return bool(s) and s[-1] in "\u3002\uff01\uff1f\uff0e\u2026"


def _ends_strong_sentence(s: str, lang_code: str | None) -> bool:
    if not s:
        return False
    if _lang_uses_dense_script(lang_code):
        return _has_cjk_sentence_terminal(s)
    return s[-1] in _STRONG_SENT_END


def _ensure_clause_end_for_tts(s: str, lang_code: str | None) -> str:
    """В конец фрагмента без границы — ASCII . или китайская 。 (для zh иначе 400)."""
    s = s.strip()
    if not s:
        return s
    if _lang_uses_dense_script(lang_code):
        if _has_cjk_sentence_terminal(s):
            return s
        while s.endswith("."):
            s = s[:-1].rstrip()
        return s + "\u3002"
    return s if _ends_with_punct(s) else s + "."


def _chunk_text(text: str, lang_code: str | None = None, voice_name: str | None = None) -> list[str]:
    """
    Разбить текст на чанки для Google TTS.
    Правила:
      1. Каждый чанк <= _TTS_MAX_BYTES байт UTF-8.
      2. Каждый чанк содержит только «короткие» предложения (лимит зависит от языка).
      3. Если текст не имеет знаков препинания — разбиваем по запятым/словам
         и добавляем точку в конец каждого чанка, чтобы Google не считал его
         одним бесконечным предложением.
    """
    text = _sanitize_for_tts(text, lang_code)
    if not text:
        return []

    max_sent = _tts_sentence_char_limit(lang_code, voice_name)
    max_chunk_chars = _tts_chunk_char_limit(lang_code, voice_name)

    # Шаг 1: разбить по знакам конца предложения (в т.ч. 。 без пробела после)
    raw = _split_sentences_for_tts(text, lang_code, voice_name)
    sentences = [s.strip() for s in raw if s.strip()]

    # Шаг 2: разбить предложения длиннее max_sent по запятым/словам
    small: list[str] = []
    for sent in sentences:
        if len(sent) <= max_sent:
            small.append(_ensure_clause_end_for_tts(sent, lang_code))
        else:
            # Сначала запятая / полноширинная ，、顿号 (без пробела после — raw r"" тут не годится для \u)
            for i, splitter in enumerate(
                (_RE_TTS_PAUSE_BREAK, _RE_TTS_COMMA_ASCII_SP, _RE_TTS_WS)
            ):
                parts = [x.strip() for x in splitter.split(sent) if x.strip()]
                if i == 0 and len(parts) <= 1:
                    continue
                buf, bufs = "", []
                for p in parts:
                    cand = (buf + " " + p).strip() if buf else p
                    if len(cand) <= max_sent:
                        buf = cand
                    else:
                        if buf:
                            bufs.append(buf)
                        buf = p
                if buf:
                    bufs.append(buf)
                if bufs and all(len(c) <= max_sent for c in bufs):
                    small.extend(_ensure_clause_end_for_tts(c, lang_code) for c in bufs)
                    break
            else:
                for i in range(0, len(sent), max_sent):
                    part = sent[i : i + max_sent]
                    small.append(_ensure_clause_end_for_tts(part, lang_code))

    # Шаг 3: упаковать в чанки — лимит и по байтам, и по символам
    result: list[str] = []
    current = ""
    for s in small:
        if current and _lang_uses_dense_script(lang_code):
            # Chirp: пустой joiner склеивает два «коротких» куска в одно длинное предложение → 400.
            if _is_chirp_voice(voice_name):
                joiner = "\n" if _ends_strong_sentence(current, lang_code) else "\u3002\n"
            else:
                joiner = "" if _ends_strong_sentence(current, lang_code) else "\u3002"
        elif current:
            joiner = " "
        else:
            joiner = ""
        cand = (current + joiner + s).strip() if current else s
        if len(cand.encode("utf-8")) <= _TTS_MAX_BYTES and len(cand) <= max_chunk_chars:
            current = cand
        else:
            if current:
                result.append(current)
            current = s
    if current:
        result.append(current)

    return result or [text]


def _synthesize_text(
    client: texttospeech.TextToSpeechClient,
    text: str,
    lang_code: str,
    voice_name: str,
) -> bytes:
    """Синтез текста любой длины: чанки → PCM → конкатенация."""
    chunks = _chunk_text(text, lang_code, voice_name)
    if not chunks:
        raise ValueError("После очистки текста не осталось символов для озвучки")
    if len(chunks) == 1:
        # Короткий текст — MP3 напрямую, без ffmpeg
        return _synthesize_segment(
            client, chunks[0], lang_code, voice_name,
            texttospeech.AudioEncoding.MP3,
        )
    # Длинный текст — LINEAR16 + склейка PCM + конвертация в MP3
    pcm_parts = [
        _wav_to_pcm(
            _synthesize_segment(
                client, chunk, lang_code, voice_name,
                texttospeech.AudioEncoding.LINEAR16,
            )
        )
        for chunk in chunks
    ]
    return _pcm_to_mp3(b"".join(pcm_parts))


# ---------------------------------------------------------------------------
# Синтез
# ---------------------------------------------------------------------------

def synthesize_simple(text: str, lang_code: str, voice_name: str) -> bytes:
    """Убрать тайминг, озвучить весь текст (с автоматической разбивкой на чанки)."""
    clean = strip_timing(text)
    if not clean:
        raise ValueError("Текст пустой после удаления тайминга")
    client = texttospeech.TextToSpeechClient()
    return _synthesize_text(client, clean, lang_code, voice_name)


def synthesize_timed(
    text: str,
    lang_code: str,
    voice_name: str,
    progress_cb=None,
) -> bytes:
    """
    Соблюдать тайминг: вставлять паузы между сегментами.
    Возвращает MP3.
    """
    segments = parse_timed_segments(text)
    if not segments:
        raise ValueError("Не удалось распознать текст или тайминг")

    client = texttospeech.TextToSpeechClient()
    pcm_parts: list[bytes] = []
    cursor = 0.0  # текущая позиция в секундах

    for idx, (start_sec, seg_text) in enumerate(segments):
        seg_text = seg_text.strip()
        if not seg_text:
            if start_sec > cursor + 0.05:
                gap = start_sec - cursor
                pcm_parts.append(_silence(gap))
                cursor = start_sec
            continue

        if progress_cb:
            progress_cb(idx, len(segments), seg_text[:40])

        # Вставить паузу если нужно
        gap = start_sec - cursor
        if gap > 0.05:
            pcm_parts.append(_silence(gap))
            cursor += gap

        # TTS для сегмента — чанкуем если длиннее лимита
        seg_chunks = _chunk_text(seg_text, lang_code, voice_name)
        if not seg_chunks:
            continue
        seg_pcm = b"".join(
            _wav_to_pcm(
                _synthesize_segment(
                    client, chunk, lang_code, voice_name,
                    texttospeech.AudioEncoding.LINEAR16,
                )
            )
            for chunk in seg_chunks
        )
        pcm_parts.append(seg_pcm)
        cursor += _pcm_duration(seg_pcm)

    full_pcm = b"".join(pcm_parts)
    return _pcm_to_mp3(full_pcm)


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------

app = FastAPI(title="Text-to-Audio")

from local_access import LocalOnly
app.add_middleware(LocalOnly)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[], allow_methods=["*"], allow_headers=["*"],
)

FRONTEND_DIR = ROOT / "frontend"

app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


class SynthesizeRequest(BaseModel):
    text: str
    language: str = "ru"
    voice: str = ""
    mode: str = "simple"   # "simple" | "timed"


@app.get("/voices/{language}")
def list_voices(language: str):
    lang_code = LANG_CODES.get(language, language)
    return {"voices": get_voices_for_lang(lang_code)}


@app.get("/languages")
def list_languages():
    return {
        "languages": [
            {"code": "es",  "label": "Испанский"},
            {"code": "en",  "label": "Английский"},
            {"code": "hi",  "label": "Хинди"},
            {"code": "ar",  "label": "Арабский"},
            {"code": "pt",  "label": "Португальский"},
            {"code": "ru",  "label": "Русский"},
            {"code": "zh",  "label": "Китайский"},
            {"code": "ja",  "label": "Японский"},
            {"code": "de",  "label": "Немецкий"},
            {"code": "bn",  "label": "Бенгальский"},
            {"code": "fr",  "label": "Французский"},
            {"code": "fil", "label": "Филиппинский"},
            {"code": "vi",  "label": "Вьетнамский"},
            {"code": "tr",  "label": "Турецкий"},
            {"code": "fa",  "label": "Персидский"},
            {"code": "id",  "label": "Индонезийский"},
            {"code": "ko",  "label": "Корейский"},
            {"code": "it",  "label": "Итальянский"},
            {"code": "th",  "label": "Тайский"},
            {"code": "pa",  "label": "Пенджаби"},
            {"code": "te",  "label": "Телугу"},
            {"code": "ms",  "label": "Малайский"},
            {"code": "ta",  "label": "Тамильский"},
            {"code": "mr",  "label": "Маратхи"},
            {"code": "nl",  "label": "Нидерландский"},
            {"code": "pl",  "label": "Польский"},
            {"code": "uk",  "label": "Украинский"},
            {"code": "yue", "label": "Кантонский"},
            {"code": "gu",  "label": "Гуджарати"},
            {"code": "uz",  "label": "Узбекский"},
            {"code": "sw",  "label": "Суахили"},
            {"code": "ro",  "label": "Румынский"},
            {"code": "cs",  "label": "Чешский"},
            {"code": "yo",  "label": "Йоруба"},
            {"code": "hu",  "label": "Венгерский"},
            {"code": "sv",  "label": "Шведский"},
            {"code": "am",  "label": "Амхарский"},
            {"code": "el",  "label": "Греческий"},
            {"code": "bg",  "label": "Болгарский"},
            {"code": "sr",  "label": "Сербский"},
            {"code": "he",  "label": "Иврит"},
            {"code": "kk",  "label": "Казахский"},
            {"code": "af",  "label": "Африкаанс"},
            {"code": "hr",  "label": "Хорватский"},
            {"code": "fi",  "label": "Финский"},
            {"code": "da",  "label": "Датский"},
            {"code": "nb",  "label": "Норвежский"},
            {"code": "sk",  "label": "Словацкий"},
            {"code": "lt",  "label": "Литовский"},
            {"code": "sl",  "label": "Словенский"},
        ]
    }


@app.post("/synthesize")
def synthesize(req: SynthesizeRequest):
    lang_code = LANG_CODES.get(req.language, req.language)
    voices = get_voices_for_lang(lang_code)
    voice_name = req.voice or voices[0]["name"]

    try:
        if req.mode == "timed":
            mp3_bytes = synthesize_timed(req.text, lang_code, voice_name)
        else:
            mp3_bytes = synthesize_simple(req.text, lang_code, voice_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка TTS: {e}")

    filename = f"audio_{uuid.uuid4().hex[:8]}.mp3"
    out_path = OUTPUT_DIR / filename
    out_path.write_bytes(mp3_bytes)

    return {"filename": filename}


@app.get("/download/{filename}")
def download(filename: str):
    if re.fullmatch(r"audio_[0-9a-f]{8}\.mp3", filename) is None:
        raise HTTPException(status_code=404, detail="Файл не найден")
    output_root = OUTPUT_DIR.resolve()
    path = (output_root / filename).resolve()
    if path.parent != output_root or not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(path, media_type="audio/mpeg", filename=filename)


@app.get("/", response_class=HTMLResponse)
def root():
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


if __name__ == "__main__":
    import errno
    import sys

    try:
        # Передаём объект app — работает при любом cwd (не только из папки text-to-audio).
        uvicorn.run(app, host="127.0.0.1", port=8767, reload=False)
    except OSError as e:
        winerr = getattr(e, "winerror", None)
        addr_in_use = winerr == 10048 or e.errno in (errno.EADDRINUSE, 10048)
        if addr_in_use:
            print(
                "\nПорт 8767 занят — сервер уже запущен или его держит другая программа.\n"
                "Закройте окно Text-to-Audio или завершите лишний процесс python.exe "
                "в диспетчере задач, затем запустите снова.\n",
                file=sys.stderr,
            )
            sys.exit(1)
        raise
