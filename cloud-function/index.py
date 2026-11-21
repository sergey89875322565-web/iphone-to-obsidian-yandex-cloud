# -*- coding: utf-8 -*-
"""
voice-inbox — HTTP-приёмник записей с iPhone (Yandex Cloud Function).

GET /?token=<INBOX_TOKEN>&name=<имя файла>  ->  presigned PUT URL (text/plain)

Возвращает presigned PUT URL на Object Storage (префикс inbox/) ПРОСТЫМ
ТЕКСТОМ — так iOS-шорткату не нужен разбор JSON. Клиент делает PUT файла
по этому URL — обходим лимит размера тела запроса к функции.

SigV4-подпись реализована вручную (без boto3), зависимостей нет.

Переменные окружения (задаются в настройках функции, НЕ в коде):
  INBOX_TOKEN — секретный токен (единственная защита публичной функции)
  S3_KEY_ID   — key_id статического ключа сервисного аккаунта
  S3_SECRET   — secret статического ключа
  BUCKET      — имя бакета Object Storage
"""

import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import quote

BUCKET = os.environ.get("BUCKET", "")
REGION = "ru-central1"
HOST = "storage.yandexcloud.net"
TOKEN = os.environ.get("INBOX_TOKEN", "")
KEY_ID = os.environ.get("S3_KEY_ID", "")
SECRET = os.environ.get("S3_SECRET", "")


def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def presign_put(key, expires=3600):
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    scope = f"{datestamp}/{REGION}/s3/aws4_request"
    canon_uri = "/" + quote(key, safe="/~")
    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{KEY_ID}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    canon_query = "&".join(
        f"{quote(k, safe='~')}={quote(v, safe='~')}"
        for k, v in sorted(params.items()))
    canon_req = "\n".join([
        "PUT", canon_uri, canon_query,
        f"host:{BUCKET}.{HOST}\n", "host", "UNSIGNED-PAYLOAD",
    ])
    str_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canon_req.encode("utf-8")).hexdigest(),
    ])
    k = _hmac(("AWS4" + SECRET).encode("utf-8"), datestamp)
    k = _hmac(k, REGION)
    k = _hmac(k, "s3")
    k = _hmac(k, "aws4_request")
    sig = hmac.new(k, str_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"https://{BUCKET}.{HOST}{canon_uri}?{canon_query}&X-Amz-Signature={sig}"


def _resp(code, payload):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(payload, ensure_ascii=False),
    }


def handler(event, context):
    q = event.get("queryStringParameters") or {}
    if not TOKEN or q.get("token") != TOKEN:
        return _resp(403, {"error": "forbidden"})
    if not KEY_ID or not SECRET or not BUCKET:
        return _resp(500, {"error": "s3 credentials not configured"})

    name = (q.get("name") or "recording.m4a").strip()
    name = name.replace("\\", "/").split("/")[-1] or "recording.m4a"
    if "." in name:
        base, ext = name.rsplit(".", 1)
    else:
        base, ext = name, "m4a"
    ext = re.sub(r"[^a-zA-Z0-9]", "", ext.lower()) or "m4a"
    base = re.sub(r"[^\w\- ]+", "", base, flags=re.U).strip()
    base = re.sub(r"\s+", "-", base) or "recording"

    now = datetime.now(timezone.utc)
    key = f"inbox/{now:%Y-%m-%d}/{now:%H%M%S}-{base}.{ext}"
    url = presign_put(key)
    # iOS Shortcuts: отдаём ссылку простым текстом — шорткату не нужен
    # разбор JSON, «Получить содержимое URL» сразу получает строку URL.
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/plain; charset=utf-8"},
        "body": url,
    }
