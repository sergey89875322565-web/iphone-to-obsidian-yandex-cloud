# -*- coding: utf-8 -*-
"""
meetly-speechkit-sync.py — синхронизация облачных расшифровок в Obsidian.

⚠️ НАСТРОЙТЕ ПОД СЕБЯ: пути к хранилищу Obsidian, папке заметок и имени
бакета задаются через переменные окружения (см. .env.example в корне
репозитория). Блок КОНФИГУРАЦИЯ ниже — единственное место, которое нужно
трогать.

Что делает:
1. Обрабатывает inbox Object Storage: записи, присланные с iPhone через
   Cloud Function (presigned PUT в s3://<BUCKET>/inbox/), расшифровываются
   через SpeechKit (meetly-speechkit.py) и превращаются в заметки
   «<NOTES_DIR>/<дата> — <имя> (запись с iPhone).md»; обработанные объекты
   переносятся в inbox-processed/.
2. Опционально (если задан LOCAL_RECORDINGS): проходит по локальным папкам
   записей вида <папка>/audio.mp4 + metadata.json (формат приложения
   Meetily), распознаёт новые и дописывает текст в заметку того же дня.

Идемпотентен: повторный запуск обрабатывает только новые записи.
Запуск:  python meetly-speechkit-sync.py [--min-seconds 45] [--dry-run]
"""

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "meetly_speechkit",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "meetly-speechkit.py"))
ms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ms)

# --- КОНФИГУРАЦИЯ (настройте под себя) -------------------------------------
# Корень хранилища Obsidian (обязательно)
VAULT = Path(os.environ.get("OBSIDIAN_VAULT", "")).expanduser()
# Папка заметок внутри хранилища
NOTES_DIR = os.environ.get("NOTES_DIR", "Meetings")
# Необязательно: папка локальных записей (audio.mp4 + metadata.json).
# Пустое значение = локальная ветка отключена, обрабатывается только inbox.
LOCAL_RECORDINGS = os.environ.get("LOCAL_RECORDINGS", "")
# Сдвиг часового пояса для времени в заметках (метки в ключах S3 — UTC).
MSK_OFFSET_HOURS = int(os.environ.get("TZ_OFFSET_HOURS", "3"))
# ---------------------------------------------------------------------------

if not str(VAULT) or not VAULT.is_dir():
    sys.exit("Задайте OBSIDIAN_VAULT — путь к хранилищу Obsidian")
NOTES = VAULT / NOTES_DIR
NOTES.mkdir(parents=True, exist_ok=True)
RECORDINGS = Path(LOCAL_RECORDINGS).expanduser() if LOCAL_RECORDINGS else None

SECTION_PREFIX = "## Расшифровка SpeechKit (облачная)"
MAX_NOTE_DISTANCE_MIN = 90  # дальше — считаем, что заметка не про эту запись

INBOX_PREFIX = "inbox/"
INBOX_DONE = "inbox-processed/"
TMP_INBOX = Path(tempfile.gettempdir()) / "speechkit-inbox"


def process_inbox(dry_run=False):
    """Записи из s3://<BUCKET>/inbox/ (приёмник с iPhone):
    скачать -> расшифровать -> заметка в NOTES_DIR -> перенести объект
    в inbox-processed/. Возвращает (done, errors)."""
    import datetime
    done = errors = 0
    s3 = ms.get_s3()
    try:
        objs = s3.list_objects_v2(Bucket=ms.BUCKET,
                                  Prefix=INBOX_PREFIX).get("Contents", [])
    except Exception as e:
        print(f"INBOX: ошибка листинга бакета: {e}")
        return 0, 1
    AUDIO_EXT = (".m4a", ".mp3", ".wav", ".ogg", ".aac", ".mp4",
                 ".flac", ".caf", ".aiff", ".opus")
    objs = [o for o in objs if o["Key"] != INBOX_PREFIX
            and not o["Key"].endswith("/")
            and o["Key"].lower().endswith(AUDIO_EXT)]
    if not objs:
        return 0, 0
    TMP_INBOX.mkdir(parents=True, exist_ok=True)

    for obj in sorted(objs, key=lambda o: o["Key"]):
        key = obj["Key"]
        m = re.match(r"inbox/(\d{4}-\d{2}-\d{2})/(\d{2})(\d{2})(\d{2})-(.+)",
                     key)
        if not m:
            print(f"INBOX: пропуск (непонятный ключ): {key}")
            continue
        date_utc, hh, mm, _ss, fname = m.groups()
        # метка в ключе — UTC; в заметку — локальное время (+TZ_OFFSET_HOURS)
        dt = (datetime.datetime.strptime(f"{date_utc} {hh}:{mm}",
                                         "%Y-%m-%d %H:%M")
              + datetime.timedelta(hours=MSK_OFFSET_HOURS))
        date_loc = dt.strftime("%Y-%m-%d")
        time_loc = dt.strftime("%H:%M")
        base = re.sub(r"\.\w+$", "", fname).replace("-", " ").strip()
        print(f"INBOX === {key} -> {date_loc} {time_loc} «{base}»")
        if dry_run:
            continue
        local = TMP_INBOX / fname
        try:
            s3.download_file(ms.BUCKET, key, str(local))
            wav = local.with_suffix(".wav")
            ms.to_wav(str(local), str(wav))
            import time as _time
            s3_key = (f"{ms.S3_PREFIX}{_time.strftime('%Y%m%d-%H%M%S')}"
                      f"-inbox-{fname}.wav")
            try:
                segments = ms.recognize(str(wav), s3_key, ms.get_api_key())
            finally:
                if wav.exists():
                    wav.unlink()
            if not segments:
                print("    ОШИБКА: пустой результат распознавания")
                errors += 1
                continue
            text = ms.render(segments)
            note = NOTES / f"{date_loc} — {base} (запись с iPhone).md"
            if note.exists():
                upsert_section(note, f"{SECTION_PREFIX} — {time_loc}", text)
            else:
                note.write_text(
                    "---\n"
                    f"date: {date_loc}\n"
                    f"time: \"{time_loc}\"\n"
                    "source: iPhone (вебхук) + SpeechKit\n"
                    "tags: [встречи, speechkit]\n"
                    "---\n\n"
                    f"# {base} (запись с iPhone)\n\n"
                    "> Расшифровка Yandex SpeechKit v3 с разметкой спикеров. "
                    "Спикер 1 / Спикер 2 — метки диаризации; переименовать "
                    "по факту участников.\n\n"
                    + text + "\n", encoding="utf-8")
            (TMP_INBOX / (base + ".json")).write_text(
                json.dumps(segments, ensure_ascii=False, indent=2),
                encoding="utf-8")
            # переносим объект в inbox-processed/
            done_key = INBOX_DONE + key[len(INBOX_PREFIX):]
            s3.copy_object(Bucket=ms.BUCKET, Key=done_key,
                           CopySource={"Bucket": ms.BUCKET, "Key": key})
            s3.delete_object(Bucket=ms.BUCKET, Key=key)
            print(f"    -> {note.name}")
            done += 1
        except Exception as e:
            print(f"    ОШИБКА: {e}")
            errors += 1
    return done, errors


def rec_local_time(rec_dir: Path):
    """Время записи (локальное) и дата из суффикса папки _YYYY-MM-DD_HH-MM (UTC)."""
    m = re.search(r"_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})$", rec_dir.name)
    if not m:
        return None
    date, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
    total = (hh * 60 + mm + MSK_OFFSET_HOURS * 60) % (24 * 60)
    return date, f"{total // 60:02d}:{total % 60:02d}", total


def find_note(rec_dir: Path):
    """Заметка дня записи; если их несколько — ближайшая по времени,
    но не дальше MAX_NOTE_DISTANCE_MIN минут."""
    rt = rec_local_time(rec_dir)
    if not rt:
        return None
    date, _, rec_min = rt
    cands = list(NOTES.glob(f"{date} — *.md"))
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]

    def note_minutes(p):
        t = re.search(r"^time:\s*(\d{2}):(\d{2})", p.read_text(
            encoding="utf-8", errors="ignore")[:600], re.M)
        return int(t.group(1)) * 60 + int(t.group(2)) if t else None

    best, best_dist = None, None
    for p in cands:
        nm = note_minutes(p)
        if nm is None:
            continue
        d = abs(nm - rec_min)
        if best_dist is None or d < best_dist:
            best, best_dist = p, d
    if best is not None and best_dist <= MAX_NOTE_DISTANCE_MIN:
        return best
    return None


def section_title(rec_dir: Path):
    rt = rec_local_time(rec_dir)
    if rt:
        return f"{SECTION_PREFIX} — {rt[1]}"
    return SECTION_PREFIX


def upsert_section(note_path: Path, title: str, text: str):
    body = note_path.read_text(encoding="utf-8")
    block = f"{title}\n\n{text}\n"
    if title in body:
        body = re.sub(re.escape(title) + r".*?(?=\n## |\Z)",
                      block, body, flags=re.S)
    else:
        body = body.rstrip() + "\n\n---\n\n" + block
    note_path.write_text(body, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-seconds", type=float, default=45)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--attach", action="store_true",
                    help="только дописать уже готовые transcript-speechkit.txt "
                         "в заметки, без новых распознаваний")
    args = ap.parse_args()

    print("--- Inbox (записи с iPhone через вебхук) ---")
    in_done, in_err = process_inbox(dry_run=args.dry_run)
    print(f"Inbox: {in_done} новых, {in_err} ошибок\n")

    if not RECORDINGS or not RECORDINGS.is_dir():
        print("Локальные записи: LOCAL_RECORDINGS не задан — пропуск")
        print(f"\nГотово: {in_done} новых, 0 пропущено, {in_err} ошибок")
        return 0 if in_err == 0 else 1

    done = skipped = errors = in_err
    for rec in sorted(RECORDINGS.iterdir()):
        audio = rec / "audio.mp4"
        meta = rec / "metadata.json"
        out_txt = rec / "transcript-speechkit.txt"
        if not audio.exists() or not meta.exists():
            continue
        if out_txt.exists():
            if args.attach:
                note = find_note(rec)
                title = section_title(rec)
                if note:
                    body = note.read_text(encoding="utf-8", errors="ignore")
                    if title in body:
                        continue
                    upsert_section(note, title,
                                   out_txt.read_text(encoding="utf-8"))
                    print(f"ATTACH: {rec.name} -> {note.name}")
                    done += 1
            else:
                skipped += 1
            continue
        try:
            dur = json.load(open(meta, encoding="utf-8")).get(
                "duration_seconds") or 0
        except Exception:
            continue
        if dur < args.min_seconds:
            skipped += 1
            continue

        print(f"=== {rec.name} ({dur:.0f} с)")
        if args.dry_run:
            continue
        try:
            rc = ms_run(rec, out_txt)
            if rc != 0:
                errors += 1
                continue
            note = find_note(rec)
            if note:
                upsert_section(note, section_title(rec),
                               out_txt.read_text(encoding="utf-8"))
                print(f"    -> {note.name}")
            else:
                print("    -> заметка дня не найдена, текст оставлен рядом "
                      "с записью")
            done += 1
        except Exception as e:
            print(f"    ОШИБКА: {e}")
            errors += 1

    print(f"\nГотово: {done} новых, {skipped} пропущено, {errors} ошибок")
    return 0 if errors == 0 else 1


def ms_run(rec: Path, out_txt: Path):
    """Обёртка над meetly-speechkit без argparse."""
    wav = rec / "_speechkit_tmp.wav"
    ms.to_wav(str(rec / "audio.mp4"), str(wav))
    api_key = ms.get_api_key()
    import time
    s3_key = f"{ms.S3_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}-{rec.name}.wav"
    try:
        segments = ms.recognize(str(wav), s3_key, api_key)
    finally:
        if wav.exists():
            wav.unlink()
    if not segments:
        return 2
    text = ms.render(segments)
    out_txt.write_text(text + "\n", encoding="utf-8")
    (rec / "transcript-speechkit.json").write_text(
        json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
