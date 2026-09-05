"""Controlled YI Home cloud validation derived from the bundled Android APK."""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import yi_model_registry


APP_VERSION = "5.6.5_20220819031350"
APP_VERSION_CODE = 319
PASSWORD_HMAC_KEY = b"KXLiUdAsO81ycDyEJAeETC$KklXdz3AC"
GATEWAY_HOSTS = {
    "cn": "https://api.xiaoyi.com",
    "us": "https://gw-us.xiaoyi.com",
    "eu": "https://gw-eu.xiaoyi.com",
    "sea": "https://gw-sg.xiaoyi.com",
}
EMAIL_RE = re.compile(r"[A-Z0-9a-z._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,4}\Z")
SECRET_FIELDS = {"password", "token", "token_secret", "userToken", "userTokenSecret"}
WRAPPER_FIELDS = {"data", "result", "response", "payload", "body"}
MESSAGE_FIELDS = {"message", "msg", "error", "error_message", "errormessage", "detail", "description", "reason"}
KNOWN_CONTENT_ENCODINGS = {"gzip", "br", "deflate", "identity", "compress", "zstd"}
KNOWN_MEDIA_TYPES = {
    "application/json",
    "application/octet-stream",
    "application/xml",
    "text/html",
    "text/json",
    "text/plain",
    "text/xml",
}
KNOWN_CHARSETS = {"utf-8", "utf8", "us-ascii", "iso-8859-1", "latin-1", "utf-16", "utf-16le", "utf-16be"}
KNOWN_MODEL_NAMES = frozenset(
    candidate.model
    for raw_model in yi_model_registry.all_server_models()
    for candidate in yi_model_registry.candidates(raw_model)
)
CAMERA_FIELD_TYPES = {
    "uid": ("string",),
    "did": ("string",),
    "model": ("string",),
    "name": ("string",),
    "message": ("string",),
    "flag": ("boolean",),
    "share": ("boolean",),
    "hasPincode": ("boolean",),
    "category": ("integer",),
    "count": ("integer",),
    "nickname": ("string",),
    "type": ("integer",),
    "accessRight": ("integer",),
    "accessCount": ("integer",),
    "sharedBy": ("integer",),
    "sharedTime": ("integer",),
    "lastAccessTime": ("integer",),
    "online": ("boolean",),
    "state": ("integer",),
    "password": ("string",),
    "groupBindable": ("integer",),
    "ipcParam": ("string", "object"),
    "appParam": ("string", "object"),
}
IPC_PARAM_FIELD_TYPES = {
    "p2p_encrypt": ("boolean", "canonical_boolean_string"),
    "ssid": ("string",),
    "mac": ("string",),
    "ip": ("string",),
    "rssi": ("integer",),
    "smartservice": ("boolean",),
    "signal_quality": ("string",),
    "signalQuality": ("string",),
    "battery": ("string",),
    "batteryLevel": ("string",),
    "battery_chg": ("string",),
    "wakeup": ("boolean",),
}


class YiCloudError(RuntimeError):
    """A safe, classified failure suitable for a sanitized report."""

    def __init__(self, category: str, message: str, diagnostic: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.category = category
        self.safe_message = message
        self.diagnostic = dict(diagnostic) if diagnostic else None


def password_transform(password: str) -> str:
    digest = hmac.new(PASSWORD_HMAC_KEY, password.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def sign_params(params: Sequence[tuple[str, str]], token: str, token_secret: str) -> str:
    canonical = "&".join(f"{key}={value}" for key, value in params)
    key = f"{token}&{token_secret}".encode("utf-8")
    digest = hmac.new(key, canonical.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def login_params(region: str, account: str, password: str, brand: str, model: str, android: str) -> list[tuple[str, str]]:
    params = [("seq", "1")]
    if region != "cn":
        params.append(("account", account))
    elif EMAIL_RE.fullmatch(account):
        params.append(("email", account))
    else:
        params.append(("mobile", account))
    params.extend(
        (
            ("password", password_transform(password)),
            ("dev_name", brand),
            ("dev_type", model),
            ("dev_os_version", f"Android {android}"),
        )
    )
    return params


def device_list_params(user_id: str, token: str, token_secret: str) -> list[tuple[str, str]]:
    signed = [("seq", "1"), ("userid", user_id)]
    return [*signed, ("hmac", sign_params(signed, token, token_secret))]


def tnp_device_info_params(user_id: str, uid: str, token: str, token_secret: str) -> list[tuple[str, str]]:
    signed = [("seq", "1"), ("userid", user_id), ("uid", uid)]
    return [*signed, ("hmac", sign_params(signed, token, token_secret))]


def request_headers(country: str, model: str, android: str, language: str) -> dict[str, str]:
    return {
        "x-kamihome-appType": "ANDROID",
        "x-kamihome-packageType": "RELEASE",
        "User-Agent": f"yihome/{APP_VERSION} ({model}; Android {android}; {language})",
        "x-xiaoyi-appCountryCode": country,
        "x-xiaoyi-appVersion": f"android;{APP_VERSION_CODE};{APP_VERSION}",
    }


def _diagnostic(
    host: str,
    path: str,
    http_status: int | None,
    yi_code: int | None,
    ok: bool,
    category: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "endpoint": path,
        "gateway": host,
        "http_status": http_status,
        "yi_code": yi_code,
        "ok": ok,
    }
    if category:
        result["error_category"] = category
    return result


def _safe_failure_message(category: str) -> str:
    return {
        "unsupported_api": "The configured YI endpoint or method appears unsupported.",
        "invalid_credentials": "YI rejected the login credentials.",
        "session_expired": "The YI cloud session expired.",
        "mfa_or_challenge": "YI appears to require an MFA, CAPTCHA, or verification challenge.",
        "rate_limit": "YI rate-limited the request.",
        "server_rejection": "YI rejected the request; use the HTTP status and YI code for diagnosis.",
        "unexpected_response_schema": "YI returned a response that does not match the APK-derived schema.",
        "transport_error": "The HTTPS request failed before a valid YI response was received.",
        "configuration_error": "The probe configuration is incomplete or invalid.",
        "report_write_failed": "The sanitized report could not be written to the requested path.",
    }[category]


def _failure_category(http_status: int | None, server_message: str, auth_request: bool) -> str:
    message = server_message.casefold()
    if http_status in {404, 405, 410, 501} or "unsupported api" in message:
        return "unsupported_api"
    if http_status == 429 or any(word in message for word in ("rate limit", "too many", "frequent")):
        return "rate_limit"
    if auth_request and any(word in message for word in ("captcha", "challenge", "verification", "verify code", "mfa", "otp", "2fa")):
        return "mfa_or_challenge"
    if auth_request and (
        http_status in {401, 403}
        or any(word in message for word in ("invalid password", "wrong password", "invalid credential", "account or password"))
    ):
        return "invalid_credentials"
    return "server_rejection"


def _response_failure_category(
    http_status: int | None,
    yi_code: int | None,
    server_message: str,
    auth_request: bool,
) -> str:
    if not auth_request and yi_code == 20202:
        return "session_expired"
    return _failure_category(http_status, server_message, auth_request)


def _json_value(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _json_object(raw: bytes) -> dict[str, Any] | None:
    value = _json_value(raw)
    return value if isinstance(value, dict) else None


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return "unknown"


def _validation_type(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    return _json_type(value)


def _field_failures(value: Mapping[str, Any], expected_fields: Mapping[str, tuple[str, ...]], prefix: str = "") -> list[dict[str, Any]]:
    failures = []
    for field, expected in expected_fields.items():
        if field not in value:
            continue
        actual = _validation_type(value[field])
        if "canonical_boolean_string" in expected and isinstance(value[field], str) and value[field].casefold() in {"true", "false"}:
            actual = "canonical_boolean_string"
        if actual not in expected:
            failures.append(
                {
                    "field": f"{prefix}{field}",
                    "expected": list(expected),
                    "actual_json_type": actual,
                }
            )
    return failures


def _field_inventory(value: Mapping[str, Any], documented_fields: Mapping[str, tuple[str, ...]]) -> tuple[list[str], list[str]]:
    missing = sorted(field for field in documented_fields if field not in value)
    additional = sorted(_safe_key_name(field) for field in value if field not in documented_fields)
    return missing, additional


def _ipc_param_shape(value: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    shape: dict[str, Any] = {"outer_type": _json_type(value)}
    failures: list[dict[str, Any]] = []
    if isinstance(value, str):
        try:
            inner = json.loads(value)
        except json.JSONDecodeError:
            shape["json_parse"] = False
            failures.append({"field": "ipcParam", "reason": "inner_json_parse_failed"})
            return shape, failures
        shape["json_parse"] = True
    elif isinstance(value, dict):
        inner = value
        shape["json_parse"] = None
        shape["representation"] = "already_object"
    else:
        shape["json_parse"] = None
        return shape, failures

    shape["inner_type"] = _json_type(inner)
    if not isinstance(inner, dict):
        failures.append(
            {
                "field": "ipcParam",
                "expected_inner": ["object"],
                "actual_inner_type": _json_type(inner),
            }
        )
        return shape, failures

    shape["keys"] = {_safe_key_name(key): _json_type(item) for key, item in inner.items()}
    missing, additional = _field_inventory(inner, IPC_PARAM_FIELD_TYPES)
    shape["missing_documented_fields"] = missing
    shape["additional_fields"] = additional
    failures.extend(_field_failures(inner, IPC_PARAM_FIELD_TYPES, "ipcParam."))
    return shape, failures


def _json_structure(value: Any, depth: int = 0) -> dict[str, Any]:
    """Describe nested JSON without retaining any scalar values."""
    shape: dict[str, Any] = {"type": _json_type(value)}
    if depth >= 4:
        return shape
    if isinstance(value, dict):
        shape["keys"] = {_safe_key_name(key): _json_type(item) for key, item in value.items()}
        nested = {
            _safe_key_name(key): _json_structure(item, depth + 1)
            for key, item in value.items()
            if isinstance(item, (dict, list))
        }
        if nested:
            shape["nested"] = nested
    elif isinstance(value, list):
        shape["count"] = len(value)
        shape["element_types"] = sorted({_json_type(item) for item in value})
    return shape


def _app_param_shape(value: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    shape: dict[str, Any] = {"outer_type": _json_type(value)}
    failures: list[dict[str, Any]] = []
    if isinstance(value, str):
        try:
            inner = json.loads(value)
        except json.JSONDecodeError:
            shape["json_parse"] = False
            failures.append({"field": "appParam", "reason": "inner_json_parse_failed"})
            return shape, failures
        shape["json_parse"] = True
    elif isinstance(value, dict):
        inner = value
        shape["json_parse"] = None
        shape["representation"] = "already_object"
    else:
        shape["json_parse"] = None
        return shape, failures
    shape["inner_shape"] = _json_structure(inner)
    if not isinstance(inner, dict):
        failures.append(
            {
                "field": "appParam",
                "expected_inner": ["object"],
                "actual_inner_type": _json_type(inner),
            }
        )
    return shape, failures


def _camera_array_element_shape(index: int, value: Any) -> dict[str, Any]:
    shape: dict[str, Any] = {"index": index, "type": _json_type(value)}
    if not isinstance(value, dict):
        shape["parser_failures"] = [
            {
                "field": "camera",
                "expected": ["object"],
                "actual_json_type": _validation_type(value),
            }
        ]
        return shape

    shape["keys"] = {_safe_key_name(key): _json_type(item) for key, item in value.items()}
    missing, additional = _field_inventory(value, CAMERA_FIELD_TYPES)
    shape["missing_documented_fields"] = missing
    shape["additional_fields"] = additional
    failures = _field_failures(value, CAMERA_FIELD_TYPES)
    if "ipcParam" in value:
        shape["ipcParam_shape"], ipc_failures = _ipc_param_shape(value["ipcParam"])
        failures.extend(ipc_failures)
    if "appParam" in value:
        shape["appParam_shape"], app_failures = _app_param_shape(value["appParam"])
        failures.extend(app_failures)
    shape["parser_validation"] = "failures_detected" if failures else "no_failures_for_present_fields"
    if failures:
        shape["parser_failures"] = failures
    return shape


def _safe_key_name(key: str) -> str:
    if len(key) <= 80 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", key):
        return key
    return f"<redacted-key:{hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]}>"


def _safe_scalar(value: Any, *, message: bool = False) -> Any:
    if message:
        return None if value is None else "<redacted>"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return "<redacted>"
    if re.fullmatch(r"-?\d{1,18}", value) or value.upper() in {"OK", "SUCCESS", "ERROR", "FAIL", "FAILED"}:
        return value
    return "<redacted>"


def _object_shape(value: Mapping[str, Any], depth: int = 0) -> dict[str, Any]:
    shape: dict[str, Any] = {
        "type": "object",
        "keys": {_safe_key_name(key): _json_type(item) for key, item in value.items()},
    }
    messages: dict[str, Any] = {}
    nested: dict[str, Any] = {}
    for key, item in value.items():
        folded = key.casefold()
        safe_key = _safe_key_name(key)
        if folded == "code":
            shape["code"] = {"type": _json_type(item), "value": _safe_scalar(item)}
        if folded in MESSAGE_FIELDS:
            messages[safe_key] = {"type": _json_type(item), "value": _safe_scalar(item, message=True)}
        if folded in WRAPPER_FIELDS:
            nested[safe_key] = _nested_shape(item, depth + 1)
    if messages:
        shape["sanitized_messages"] = messages
    if nested:
        shape["nested_shapes"] = nested
    return shape


def _nested_shape(value: Any, depth: int) -> dict[str, Any]:
    result: dict[str, Any] = {"type": _json_type(value)}
    if isinstance(value, dict):
        result["keys"] = {_safe_key_name(key): _json_type(item) for key, item in value.items()}
        if depth <= 4:
            details = _object_shape(value, depth)
            for key in ("code", "sanitized_messages", "nested_shapes"):
                if key in details:
                    result[key] = details[key]
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            result["string_json_parse"] = False
        else:
            result["string_json_parse"] = True
            result["string_json_type"] = _json_type(parsed)
            if isinstance(parsed, dict):
                result["string_json_shape"] = _object_shape(parsed, depth)
    return result


def _content_type(headers: Any) -> str | None:
    value = headers.get("Content-Type") if headers is not None else None
    if not isinstance(value, str) or not value.strip():
        return None
    media_type = value.split(";", 1)[0].strip().casefold()
    if not re.fullmatch(r"[a-z0-9!#$&^_.+*-]+/[a-z0-9!#$&^_.+*-]+", media_type):
        return "<invalid-or-redacted>"
    if media_type not in KNOWN_MEDIA_TYPES:
        media_type = "application/*+json" if media_type.startswith("application/") and media_type.endswith("+json") else "other"
    charset = re.search(r"(?:^|;)\s*charset\s*=\s*[\"']?([A-Za-z0-9._-]+)", value, re.IGNORECASE)
    if not charset:
        return media_type
    normalized_charset = charset.group(1).lower()
    return f"{media_type}; charset={normalized_charset if normalized_charset in KNOWN_CHARSETS else 'other'}"


def _content_encoding(headers: Any) -> str | None:
    value = headers.get("Content-Encoding") if headers is not None else None
    if not isinstance(value, str) or not value.strip():
        return None
    encodings = [item.strip().casefold() for item in value.split(",")]
    return ", ".join(item if item in KNOWN_CONTENT_ENCODINGS else "other" for item in encodings)


def _looks_like_html_or_xml(raw: bytes) -> bool:
    prefix = raw.lstrip(b"\xef\xbb\xbf\x00\x09\x0a\x0d\x20")[:128].lower()
    return prefix.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"<?xml"))


def _body_magic(raw: bytes) -> str | None:
    if raw.startswith(b"\x1f\x8b"):
        return "gzip"
    if raw.startswith(b"PK\x03\x04"):
        return "zip"
    if raw.startswith(b"\x28\xb5\x2f\xfd"):
        return "zstd"
    return None


def _encoding_observation(content_encoding: str | None, magic: str | None, utf8: bool, json_parse: bool) -> str:
    if content_encoding and "gzip" in content_encoding:
        return "gzip_bytes_remain_after_urllib_read" if magic == "gzip" else "gzip_header_body_not_gzip_framed_possibly_already_decoded"
    if content_encoding and "br" in content_encoding:
        return "br_header_present_no_manual_decode_attempted"
    if content_encoding and "zstd" in content_encoding:
        return "zstd_bytes_remain_after_urllib_read" if magic == "zstd" else "zstd_header_without_zstd_magic"
    if content_encoding:
        return "content_encoding_present_no_manual_decode_attempted"
    if magic:
        return f"{magic}_magic_without_content_encoding_header"
    if utf8 and json_parse:
        return "no_content_encoding_header_json_bytes"
    return "no_content_encoding_header"


def response_shape(raw: bytes, headers: Any = None, endpoint: str | None = None) -> dict[str, Any]:
    """Describe response structure without retaining or returning body values."""
    content_type = _content_type(headers)
    content_encoding = _content_encoding(headers)
    magic = _body_magic(raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    try:
        parsed = json.loads(text) if text is not None else None
        json_parse = text is not None
    except json.JSONDecodeError:
        parsed = None
        json_parse = False

    html_or_xml = _looks_like_html_or_xml(raw)
    if html_or_xml:
        response_kind = "html_or_xml"
    elif json_parse:
        response_kind = "json"
    elif not raw:
        response_kind = "empty"
    elif text is not None:
        response_kind = "text_non_json"
    else:
        response_kind = "binary_or_encoded_or_encrypted"

    result: dict[str, Any] = {
        "content_type": content_type,
        "content_encoding": content_encoding,
        "body_length": len(raw),
        "body_sha256": hashlib.sha256(raw).hexdigest(),
        "body_capture": "urllib_response_read",
        "body_magic": magic,
        "utf8": text is not None,
        "json_parse": json_parse,
        "response_kind": response_kind,
        "content_encoding_observation": _encoding_observation(content_encoding, magic, text is not None, json_parse),
    }
    if json_parse:
        result["json_type"] = _json_type(parsed)
        if isinstance(parsed, dict):
            shape = _object_shape(parsed)
            result["keys"] = shape["keys"]
            for key in ("code", "sanitized_messages", "nested_shapes"):
                if key in shape:
                    result[key] = shape[key]
            if endpoint == "/v4/devices/list" and isinstance(parsed.get("data"), list):
                result["array_count"] = len(parsed["data"])
                result["array_elements"] = [_camera_array_element_shape(index, item) for index, item in enumerate(parsed["data"])]
        elif endpoint == "/v4/devices/list" and isinstance(parsed, list):
            result["array_count"] = len(parsed)
            result["array_elements"] = [_camera_array_element_shape(index, item) for index, item in enumerate(parsed)]
        elif isinstance(parsed, str):
            try:
                second = json.loads(parsed)
            except json.JSONDecodeError:
                result["second_json_parse"] = False
            else:
                result["second_json_parse"] = True
                result["second_json_type"] = _json_type(second)
                if isinstance(second, dict):
                    result["second_json_shape"] = _object_shape(second)
    return result


def _response_code(payload: Mapping[str, Any] | None) -> int | None:
    if not payload:
        return None
    value = payload.get("code")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,9}", value):
        return int(value)
    return None


def _user_id(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value else None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _response_message(payload: Mapping[str, Any] | None) -> str:
    if not payload:
        return ""
    value = payload.get("msg") or payload.get("message")
    return value if isinstance(value, str) else ""


def get_json(
    host: str,
    path: str,
    params: Sequence[tuple[str, str]],
    headers: Mapping[str, str],
    timeout: float,
    *,
    auth_request: bool = False,
    debug_response_shape: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Perform a request without ever surfacing the secret-bearing URL."""
    url = f"{host}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            raw = response.read()
            response_headers = getattr(response, "headers", None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        payload = _json_object(raw)
        status = exc.code
        yi_code = _response_code(payload)
        category = _response_failure_category(status, yi_code, _response_message(payload), auth_request)
        diagnostic = _diagnostic(host, path, status, yi_code, False, category)
        if debug_response_shape:
            diagnostic.update(response_shape(raw, exc.headers, path))
        raise YiCloudError(category, _safe_failure_message(category), diagnostic) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        category = "transport_error"
        diagnostic = _diagnostic(host, path, None, None, False, category)
        raise YiCloudError(category, _safe_failure_message(category), diagnostic) from exc

    payload = _json_object(raw)
    if status != 200:
        yi_code = _response_code(payload)
        category = _response_failure_category(status, yi_code, _response_message(payload), auth_request)
        diagnostic = _diagnostic(host, path, status, yi_code, False, category)
        if debug_response_shape:
            diagnostic.update(response_shape(raw, response_headers, path))
        raise YiCloudError(category, _safe_failure_message(category), diagnostic)
    if payload is None or _response_code(payload) is None:
        category = "unexpected_response_schema"
        diagnostic = _diagnostic(host, path, status, None, False, category)
        if debug_response_shape:
            diagnostic.update(response_shape(raw, response_headers, path))
        raise YiCloudError(category, _safe_failure_message(category), diagnostic)

    yi_code = _response_code(payload)
    if yi_code != 20000:
        category = _response_failure_category(status, yi_code, _response_message(payload), auth_request)
        diagnostic = _diagnostic(host, path, status, yi_code, False, category)
        if debug_response_shape:
            diagnostic.update(response_shape(raw, response_headers, path))
        raise YiCloudError(category, _safe_failure_message(category), diagnostic)
    diagnostic = _diagnostic(host, path, status, yi_code, True)
    if debug_response_shape:
        diagnostic.update(response_shape(raw, response_headers, path))
    return payload, diagnostic


def redact(value: Any) -> Any:
    """Redact known secret fields in a JSON-compatible value."""
    if isinstance(value, dict):
        return {key: ("<redacted>" if key in SECRET_FIELDS and item else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def require(value: str | None, label: str) -> str:
    if value:
        return value
    raise YiCloudError("configuration_error", f"Missing {label}; provide its CLI option or YI_* environment variable.")


def credentials(args: argparse.Namespace) -> tuple[str, str]:
    account = args.account or os.getenv("YI_ACCOUNT") or input("YI account: ").strip()
    password = os.getenv("YI_PASSWORD") or getpass.getpass("YI password: ")
    return require(account, "account"), require(password, "password")


def _string(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return value if isinstance(value, str) else str(value)


def _int(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _bool(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def normalize_model(raw_model: Any, did: Any = None) -> str:
    model = _string(raw_model)
    resolved, _ = yi_model_registry.resolve(model, did)
    return model if resolved == "UNKNOWN" and model in KNOWN_MODEL_NAMES else resolved


def _identifier(value: Any, reveal: bool = False) -> str | None:
    text = _string(value)
    if not text:
        return None
    return text if reveal else "<redacted>"


def _ipc_params(camera: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = camera.get("ipcParam", "{}")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise YiCloudError("unexpected_response_schema", _safe_failure_message("unexpected_response_schema"))
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise YiCloudError("unexpected_response_schema", _safe_failure_message("unexpected_response_schema")) from exc
    if not isinstance(parsed, dict):
        raise YiCloudError("unexpected_response_schema", _safe_failure_message("unexpected_response_schema"))
    return parsed


def _p2p_encrypt(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.casefold() == "true":
            return True
        if value.casefold() == "false":
            return False
    raise YiCloudError("unexpected_response_schema", _safe_failure_message("unexpected_response_schema"))


def _decrypt_camera_password(uid: str, encrypted_password: str) -> str:
    if len(uid) < 16:
        raise ValueError("UID is too short for the APK-derived AES key")
    key = uid[:16].encode("utf-8")
    ciphertext = bytes.fromhex(encrypted_password)
    if not ciphertext or len(ciphertext) % 16:
        raise ValueError("ciphertext is not a non-empty AES block sequence")
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    return plaintext.decode("utf-8")


def _password_status(camera: Mapping[str, Any], uid: str) -> tuple[bool, bool, str]:
    if _bool(camera.get("hasPincode")):
        return False, False, "pin_required"
    encrypted = camera.get("password", "")
    if not isinstance(encrypted, str):
        return False, False, "invalid_cloud_password_field"
    if not encrypted:
        # The APK supplies literal 888888 in memory. Never return or report it here.
        return False, True, "not_required_apk_fallback"
    try:
        plaintext = _decrypt_camera_password(uid, encrypted)
        usable = bool(plaintext)
        del plaintext
        return True, usable, "decrypted" if usable else "decrypted_empty_plaintext"
    except (ValueError, UnicodeDecodeError):
        return False, False, "decrypt_failed"


def camera_summary(camera: Mapping[str, Any], index: int, show_uid: bool) -> dict[str, Any]:
    uid = _string(camera.get("uid"))
    ipc = _ipc_params(camera)
    p2p_encrypt = _p2p_encrypt(ipc.get("p2p_encrypt", True))
    encrypted_recovery_succeeded, usable, recovery_status = _password_status(camera, uid)
    p2p_type = _int(camera.get("type"))
    interpretation = {0: "TUTK/Kalay", 1: "Langtao", 2: "TNP"}.get(p2p_type, "UNKNOWN")
    return {
        "index": index,
        "name": _string(camera.get("name")),
        "uid": _identifier(uid, show_uid),
        "did": _identifier(camera.get("did")),
        "raw_cloud_model": _string(camera.get("model"), "1"),
        "normalized_model": normalize_model(camera.get("model"), camera.get("did")),
        "type": p2p_type,
        "p2p_interpretation": interpretation,
        "online": _bool(camera.get("online")),
        "state": _int(camera.get("state")),
        "share": _bool(camera.get("share")),
        "hasPincode": _bool(camera.get("hasPincode")),
        "ipcParam": {
            "ip": _string(ipc.get("ip")),
            "mac": _identifier(ipc.get("mac")),
            "rssi": _int(ipc.get("rssi")),
            "p2p_encrypt": p2p_encrypt,
            "wakeup": _bool(ipc.get("wakeup")),
        },
        "encrypted_camera_password_recovery_succeeded": encrypted_recovery_succeeded,
        "camera_password_recovery_status": recovery_status,
        "p2p_descriptor_has_usable_password": usable,
    }


def tnp_info_summary(data: Mapping[str, Any], camera: Mapping[str, Any], index: int, show_uid: bool) -> dict[str, Any]:
    uid = _string(camera.get("uid"))
    did = data.get("DID")
    server = data.get("InitString")
    license_value = data.get("License")
    license_parts = license_value.split(":") if isinstance(license_value, str) else []
    return {
        "camera_index": index,
        "uid": _identifier(uid, show_uid),
        "raw_cloud_model": _string(camera.get("model")),
        "normalized_model": normalize_model(camera.get("model"), camera.get("did")),
        "response_data_keys": {_safe_key_name(key): _json_type(value) for key, value in data.items()},
        "tnp_did_available": isinstance(did, str) and bool(did),
        "tnp_server_string_available": isinstance(server, str) and bool(server),
        "tnp_license_available": isinstance(license_value, str) and bool(license_value),
        "tnp_license_device_key_available": bool(license_parts and license_parts[0]),
        "tnp_license_component_count": len(license_parts),
        "tnp_header_version_present": any(key.casefold() in {"headerversion", "header_version", "tnpheaderversion"} for key in data),
    }


def _base_report(args: argparse.Namespace) -> dict[str, Any]:
    command = "discover" if args.command == "cameras" else args.command
    return {
        "ok": False,
        "command": command,
        "region": args.region.upper(),
        "country": args.country.upper(),
        "selected_regional_gateway": GATEWAY_HOSTS[args.region],
        "requests": [],
    }


def _schema_failure(host: str, path: str, diagnostic: Mapping[str, Any]) -> YiCloudError:
    category = "unexpected_response_schema"
    failed = dict(diagnostic)
    failed.update(_diagnostic(host, path, diagnostic.get("http_status"), diagnostic.get("yi_code"), False, category))
    return YiCloudError(category, _safe_failure_message(category), failed)


def run(args: argparse.Namespace) -> dict[str, Any]:
    report = _base_report(args)
    host = GATEWAY_HOSTS[args.region]
    headers = request_headers(args.country.upper(), args.device_model, args.android_version, args.language)
    try:
        account, password = credentials(args)
        login_response, login_diagnostic = get_json(
            host,
            "/v4/users/login",
            login_params(args.region, account, password, args.device_brand, args.device_model, args.android_version),
            headers,
            args.timeout,
            auth_request=True,
            debug_response_shape=args.debug_response_shape,
        )
        report["requests"].append(login_diagnostic)
        login_data = login_response.get("data")
        if not isinstance(login_data, dict):
            raise _schema_failure(host, "/v4/users/login", login_diagnostic)
        user_id = _user_id(login_data.get("userid"))
        token = login_data.get("token")
        token_secret = login_data.get("token_secret")
        if user_id is None or not all(isinstance(value, str) and value for value in (token, token_secret)):
            raise _schema_failure(host, "/v4/users/login", login_diagnostic)

        if args.command == "login":
            report["ok"] = True
            return report

        list_response, list_diagnostic = get_json(
            host,
            "/v4/devices/list",
            device_list_params(user_id, token, token_secret),
            headers,
            args.timeout,
            debug_response_shape=args.debug_response_shape,
        )
        report["requests"].append(list_diagnostic)
        cameras = list_response.get("data")
        if not isinstance(cameras, list) or not all(isinstance(camera, dict) for camera in cameras):
            raise _schema_failure(host, "/v4/devices/list", list_diagnostic)
        report["camera_count"] = len(cameras)
        if args.command == "tnp-info":
            if args.camera_index is None or args.camera_index < 1 or args.camera_index > len(cameras):
                raise YiCloudError("configuration_error", "--camera-index must select one returned camera.")
            camera = cameras[args.camera_index - 1]
            uid = _string(camera.get("uid"))
            if not uid:
                raise _schema_failure(host, "/v4/devices/list", list_diagnostic)
            tnp_response, tnp_diagnostic = get_json(
                host,
                "/v4/tnp/device_info",
                tnp_device_info_params(user_id, uid, token, token_secret),
                headers,
                args.timeout,
                debug_response_shape=args.debug_response_shape,
            )
            report["requests"].append(tnp_diagnostic)
            tnp_data = tnp_response.get("data")
            if not isinstance(tnp_data, dict):
                raise _schema_failure(host, "/v4/tnp/device_info", tnp_diagnostic)
            report["tnp_info"] = tnp_info_summary(tnp_data, camera, args.camera_index, args.show_uid)
        else:
            try:
                report["cameras"] = [camera_summary(camera, index, args.show_uid) for index, camera in enumerate(cameras, 1)]
            except YiCloudError as exc:
                raise _schema_failure(host, "/v4/devices/list", list_diagnostic) from exc
        report["ok"] = True
        return report
    except YiCloudError as exc:
        if exc.diagnostic:
            requests = report["requests"]
            if requests and requests[-1].get("endpoint") == exc.diagnostic.get("endpoint"):
                requests[-1] = exc.diagnostic
            else:
                requests.append(exc.diagnostic)
        report["error"] = {"category": exc.category, "message": exc.safe_message}
        return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Validate the APK-derived YI Home cloud contract")
    result.add_argument("command", choices=("login", "cameras", "discover", "tnp-info"))
    result.add_argument("--region", choices=tuple(GATEWAY_HOSTS), required=True, help="Account region; never inferred")
    result.add_argument("--country", default=os.getenv("YI_COUNTRY"), required=os.getenv("YI_COUNTRY") is None, help="YI country code, for example IL")
    result.add_argument("--account", help="Defaults to YI_ACCOUNT, then an interactive prompt")
    result.add_argument("--device-brand", default=os.getenv("YI_DEVICE_BRAND"), required=os.getenv("YI_DEVICE_BRAND") is None)
    result.add_argument("--device-model", default=os.getenv("YI_DEVICE_MODEL"), required=os.getenv("YI_DEVICE_MODEL") is None)
    result.add_argument("--android-version", default=os.getenv("YI_ANDROID_VERSION"), required=os.getenv("YI_ANDROID_VERSION") is None)
    result.add_argument("--language", default=os.getenv("YI_LANGUAGE"), required=os.getenv("YI_LANGUAGE") is None, help="App locale, for example en-US")
    result.add_argument("--timeout", type=float, default=10.0)
    result.add_argument("--show-uid", action="store_true", help="Reveal full camera UIDs; DID and MAC remain redacted")
    result.add_argument("--camera-index", type=int, help="Camera to query with tnp-info (one-based devices/list index)")
    result.add_argument("--debug-response-shape", action="store_true", help="Report secret-safe response metadata and JSON shape")
    result.add_argument("--save-report", type=Path, help="Write this sanitized JSON report to a new file")
    return result


def _save_report(path: Path, report: Mapping[str, Any]) -> None:
    try:
        with path.open("x", encoding="utf-8") as destination:
            json.dump(report, destination, indent=2, sort_keys=True)
            destination.write("\n")
    except OSError as exc:
        raise YiCloudError("report_write_failed", _safe_failure_message("report_write_failed")) from exc


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    report = run(args)
    if args.save_report:
        try:
            _save_report(args.save_report, report)
        except YiCloudError as exc:
            print(json.dumps({"ok": False, "error": {"category": exc.category, "message": exc.safe_message}}, indent=2), file=sys.stderr)
            print(json.dumps(report, indent=2, sort_keys=True))
            return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
