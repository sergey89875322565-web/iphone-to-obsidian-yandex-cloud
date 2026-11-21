# -*- coding: utf-8 -*-
"""
meetly-speechkit.py — распознавание аудио через Yandex SpeechKit v3.

⚠️ НАСТРОЙТЕ ПОД СЕБЯ: все пути, имя бакета и секреты задаются через
переменные окружения (см. .env.example в корне репозитория). Значений
по умолчанию с чужими путями здесь нет — но проверьте блок КОНФИГУРАЦИЯ
ниже и дефолты на свой вкус.

Пайплайн: аудиофайл -> WAV 16k mono (ffmpeg из imageio-ffmpeg)
-> загрузка в Object Storage -> асинхронное распознавание SpeechKit API v3
(ru-RU, нормализация текста, разметка спикеров) -> текст с тайм-кодами
и спикерами.

Секреты (НЕ в git):
  - S3: JSON-файл статического ключа сервисного аккаунта, путь в
    переменной S3_SA_KEY_FILE. Структура файла:
    {"access_key": {"key_id": "YCAJE..."}, "secret": "YCMn..."}
  - API-ключ SpeechKit: переменная YC_API_KEY. Если не задана и задан
    API_KEY_FILE — читается из JSON-файла ({"secret": "AQVN..."}).

Запуск:
  python meetly-speechkit.py "<папка записи или аудиофайл>" [--compare]

Коды выхода: 0 — ок, 1 — ошибка конфигурации, 2 — ошибка распознавания.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

import boto3
from botocore.config import Config as BotoConfig

# --- КОНФИГУРАЦИЯ (настройте под себя) -------------------------------------
BUCKET = os.environ.get("S3_BUCKET", "")          # имя бакета (обязательно)
S3_PREFIX = os.environ.get("S3_PREFIX", "stt-tmp/")
# Путь к JSON со статическим ключом S3 (см. .env.example)
SA_STATIC = os.environ.get("S3_SA_KEY_FILE", "secrets/s3-sa-static.json")
# Необязательный JSON-файл с API-ключом SpeechKit (если нет YC_API_KEY)
API_KEY_FILE = os.environ.get("API_KEY_FILE", "secrets/speechkit-apikey.json")
# ---------------------------------------------------------------------------

STT_URL = "https://stt.api.cloud.yandex.net/stt/v3/recognizeFileAsync"
STT_GET = "https://stt.api.cloud.yandex.net/stt/v3/getRecognition"
OPS_URL = "https://operation.api.cloud.yandex.net/operations/"

POLL_INTERVAL = 5
POLL_TIMEOUT = 1800  # 30 мин на длинные записи


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def get_api_key():
    key = os.environ.get("YC_API_KEY", "").strip()
    if key:
        return key
    if os.path.exists(API_KEY_FILE):
        d = json.load(open(API_KEY_FILE, encoding="utf-8"))
        return d.get("secret") or d.get("apiKey", {}).get("secret", "")
    raise RuntimeError(
        "API-ключ SpeechKit не найден: задайте YC_API_KEY "
        "или файл из API_KEY_FILE")


def get_s3():
    if not BUCKET:
        raise RuntimeError("Не задан S3_BUCKET (имя бакета)")
    cred = json.load(open(SA_STATIC, encoding="utf-8"))
    return boto3.client(
        "s3",
        endpoint_url="https://storage.yandexcloud.net",
        region_name="ru-central1",
        aws_access_key_id=cred["access_key"]["key_id"],
        aws_secret_access_key=cred["secret"],
        config=BotoConfig(connect_timeout=15, read_timeout=300,
                          retries={"max_attempts": 3}),
    )


def find_ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"


def to_wav(src, dst):
    ffmpeg = find_ffmpeg()
    cmd = [ffmpeg, "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", dst]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if p.returncode != 0 or not os.path.exists(dst):
        raise RuntimeError(f"ffmpeg: {p.stderr[-500:]}")
    return dst


def api_request(method, url, api_key, payload=None, timeout=60):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Api-Key {api_key}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def recognize(wav_path, s3_key, api_key):
    s3 = get_s3()
    log(f"Загрузка в s3://{BUCKET}/{s3_key} ...")
    s3.upload_file(wav_path, BUCKET, s3_key)

    body = {
        "uri": f"https://storage.yandexcloud.net/{BUCKET}/{s3_key}",
        "recognitionModel": {
            "model": "general:rc",
            "audioFormat": {
                "containerAudio": {"containerAudioType": "WAV"}
            },
            "textNormalization": {
                "textNormalization": "TEXT_NORMALIZATION_ENABLED",
                "profanityFilter": False,
                "literatureText": True,
            },
            "languageRestriction": {
                "restrictionType": "WHITELIST",
                "languageCode": ["ru-RU"],
            },
            "audioProcessingType": "FULL_DATA",
        },
        "speakerLabeling": {"speakerLabeling": "SPEAKER_LABELING_ENABLED"},
    }
    status, resp = api_request("POST", STT_URL, api_key, body)
    if status != 200:
        raise RuntimeError(f"asyncRecognize HTTP {status}: {body_err(resp)}")
    op = json.loads(resp)
    op_id = op.get("id")
    log(f"Операция запущена: {op_id}")

    # ждём завершения операции
    t0 = time.time()
    while time.time() - t0 < POLL_TIMEOUT:
        status, resp = api_request("GET", OPS_URL + op_id, api_key)
        if status == 200:
            o = json.loads(resp)
            if o.get("done"):
                if "error" in o:
                    raise RuntimeError(f"Операция завершилась ошибкой: "
                                       f"{o['error'].get('message')}")
                break
        time.sleep(POLL_INTERVAL)
    else:
        raise RuntimeError("Таймаут ожидания распознавания")
    log(f"Распознавание завершено за {int(time.time() - t0)} с")

    # забираем результат (поток JSON-строк)
    status, resp = api_request(
        "GET", f"{STT_GET}?operation_id={op_id}", api_key, timeout=300)
    if status != 200:
        raise RuntimeError(f"getRecognition HTTP {status}: {body_err(resp)}")

    # final (сырой текст) и finalRefinement (нормализованный) приходят
    # отдельными событиями и НУМЕРУЮТСЯ независимо — поэтому связываем
    # по тайм-кодам альтернативы, а не по finalIndex.
    by_time = {}
    for line in resp.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            res = json.loads(line).get("result", {})
        except json.JSONDecodeError:
            continue
        chan = res.get("channelTag", "0")
        for kind, block in (("raw", res.get("final")),
                            ("norm", res.get("finalRefinement", {})
                                 .get("normalizedText"))):
            if not block:
                continue
            alts = block.get("alternatives", [])
            if not alts:
                continue
            a = alts[0]
            key = (int(a.get("startTimeMs", 0)), chan)
            by_time.setdefault(key, {})[kind] = a

    segments = []
    for (start_ms, chan) in sorted(by_time):
        pack = by_time[(start_ms, chan)]
        a = pack.get("norm") or pack.get("raw")
        segments.append({
            "text": a.get("text", ""),
            "start_ms": start_ms,
            "end_ms": int(a.get("endTimeMs", start_ms)),
            "speaker": a.get("speakerTag") or "",
            "channel": chan,
        })

    # Диаризация SpeechKit: при speakerLabeling метка спикера приходит
    # в channelTag. Если каналов больше одного — это спикеры; если один —
    # спикеров не разметили.
    chans = {s["channel"] for s in segments}
    if len(chans) > 1:
        for s in segments:
            if not s["speaker"]:
                s["speaker"] = str(int(s["channel"]) + 1)
    for s in segments:
        s.pop("channel", None)

    # убираем объект из бакета, чтобы не копился мусор
    try:
        s3.delete_object(Bucket=BUCKET, Key=s3_key)
    except Exception:
        pass
    return segments


def body_err(resp):
    return resp[:400]


def fmt_time(ms):
    s = ms // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


def render(segments):
    """Если разметка спикеров есть — группируем по спикерам,
    иначе просто сшиваем с тайм-кодами абзацами."""
    has_speakers = any(s["speaker"] for s in segments)
    lines = []
    if has_speakers:
        cur_spk, buf = None, []
        for seg in segments:
            spk = seg["speaker"]
            if spk != cur_spk:
                if buf:
                    lines.append(" ".join(buf))
                cur_spk = spk
                buf = [f"**Спикер {spk}** [{fmt_time(seg['start_ms'])}]: "
                       + seg["text"]]
            else:
                buf.append(seg["text"])
        if buf:
            lines.append(" ".join(buf))
    else:
        para = []
        for seg in segments:
            para.append(seg["text"])
            if seg["text"].endswith((".", "?", "!")) and len(" ".join(para)) > 300:
                lines.append(f"[{fmt_time(seg['start_ms'])}] " + " ".join(para))
                para = []
        if para:
            lines.append(" ".join(para))
    return "\n\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="папка записи или путь к аудиофайлу")
    ap.add_argument("--compare", action="store_true",
                    help="показать рядом транскрипт из transcripts.json")
    ap.add_argument("--out", help="куда сохранить .txt (по умолчанию рядом с аудио)")
    args = ap.parse_args()

    src = args.source
    if os.path.isdir(src):
        src = os.path.join(src, "audio.mp4")
    if not os.path.exists(src):
        print(f"Нет файла: {src}")
        return 1

    work = os.path.dirname(os.path.abspath(src))
    wav = os.path.join(work, "_speechkit_tmp.wav")
    log(f"Конвертация в WAV: {src}")
    to_wav(src, wav)
    size_mb = os.path.getsize(wav) / 1e6
    log(f"WAV готов: {size_mb:.1f} МБ")

    api_key = get_api_key()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    s3_key = f"{S3_PREFIX}{stamp}-{os.path.basename(work)}.wav"

    try:
        segments = recognize(wav, s3_key, api_key)
    finally:
        if os.path.exists(wav):
            os.remove(wav)

    if not segments:
        print("Пустой результат распознавания")
        return 2

    text = render(segments)
    out_txt = args.out or os.path.join(work, "transcript-speechkit.txt")
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    with open(out_txt.replace(".txt", ".json"), "w", encoding="utf-8") as f:
        json.dump(segments, f, ensure_ascii=False, indent=2)

    print("\n===== SPEECHKIT =====")
    print(text)

    if args.compare:
        tj = os.path.join(work, "transcripts.json")
        if os.path.exists(tj):
            w = json.load(open(tj, encoding="utf-8"))
            wt = "\n".join(s.get("text", "") for s in w.get("segments", []))
            print("\n===== WHISPER =====")
            print(wt)
    print(f"\nСохранено: {out_txt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
