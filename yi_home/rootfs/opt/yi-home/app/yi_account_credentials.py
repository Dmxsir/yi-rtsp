#!/usr/bin/env python3
"""Secret-safe persistent YI account credential handoff for the HA App.

The Integration sends credentials only to the bearer-authenticated App API.
Credentials are validated against YI before being persisted. YI cloud session
material (userid/token/token_secret) is never persisted or returned.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

import yi_cloud_probe as cloud

if TYPE_CHECKING:
    from yi_cloud_session import YiCloudSession


ENV_KEYS = (
    "YI_REGION",
    "YI_COUNTRY",
    "YI_ACCOUNT",
    "YI_PASSWORD",
    "YI_DEVICE_BRAND",
    "YI_DEVICE_MODEL",
    "YI_ANDROID_VERSION",
    "YI_LANGUAGE",
)
PAYLOAD_KEYS = {
    "region",
    "country",
    "account",
    "password",
    "device_brand",
    "device_model",
    "android_version",
    "language",
}
COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")
LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2})?$")


class YiAccountCredentialError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.safe_message = message


@dataclass(frozen=True, repr=False)
class YiAccountCredentials:
    region: str
    country: str
    account: str
    password: str
    device_brand: str
    device_model: str
    android_version: str
    language: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "YiAccountCredentials":
        unknown = set(payload) - PAYLOAD_KEYS
        missing = PAYLOAD_KEYS - set(payload)
        if unknown or missing:
            raise YiAccountCredentialError(
                "invalid_request",
                "The YI account configuration fields are incomplete or unsupported.",
            )

        values: dict[str, str] = {}
        for key in PAYLOAD_KEYS:
            value = payload.get(key)
            if not isinstance(value, str) or not value:
                raise YiAccountCredentialError(
                    "invalid_request",
                    "Every YI account configuration field must be a non-empty string.",
                )
            if any(char in value for char in ("\x00", "\n", "\r")) or value != value.strip():
                raise YiAccountCredentialError(
                    "invalid_request",
                    "YI account configuration contains characters that cannot be stored safely.",
                )
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                raise YiAccountCredentialError(
                    "invalid_request",
                    "YI account configuration cannot be wrapped in matching quote characters.",
                )
            limit = 4096 if key == "password" else 512
            if len(value) > limit:
                raise YiAccountCredentialError("invalid_request", "A YI account configuration field is too long.")
            values[key] = value

        region = values["region"].casefold()
        if region not in cloud.GATEWAY_HOSTS:
            raise YiAccountCredentialError("invalid_request", "Unsupported YI account region.")
        country = values["country"].upper()
        if not COUNTRY_RE.fullmatch(country):
            raise YiAccountCredentialError("invalid_request", "YI country must be a two-letter code.")
        language = values["language"]
        if not LANGUAGE_RE.fullmatch(language):
            raise YiAccountCredentialError("invalid_request", "YI language must use an app locale such as en-US.")

        return cls(
            region=region,
            country=country,
            account=values["account"],
            password=values["password"],
            device_brand=values["device_brand"],
            device_model=values["device_model"],
            android_version=values["android_version"],
            language=language,
        )

    def env(self) -> dict[str, str]:
        return {
            "YI_REGION": self.region,
            "YI_COUNTRY": self.country,
            "YI_ACCOUNT": self.account,
            "YI_PASSWORD": self.password,
            "YI_DEVICE_BRAND": self.device_brand,
            "YI_DEVICE_MODEL": self.device_model,
            "YI_ANDROID_VERSION": self.android_version,
            "YI_LANGUAGE": self.language,
        }

    def safe_status(self, *, camera_count: int | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "configured": True,
            "region": self.region,
            "country": self.country,
            "secrets_exposed": False,
        }
        if camera_count is not None:
            result["camera_count"] = camera_count
        return result


class YiAccountCredentialStore:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()

    def _configured_from_environment(self) -> bool:
        return all(bool(os.getenv(key)) for key in ENV_KEYS)

    def status(self) -> dict[str, Any]:
        configured = self._configured_from_environment() and self.path.is_file() and self.path.stat().st_size > 0
        return {
            "ok": True,
            "configured": configured,
            "region": os.getenv("YI_REGION") if configured else None,
            "country": os.getenv("YI_COUNTRY") if configured else None,
            "credential_file_mode": "0600" if configured and (self.path.stat().st_mode & 0o777) == 0o600 else None,
            "secrets_exposed": False,
        }

    @staticmethod
    def _validate_live(credentials: YiAccountCredentials, timeout: float) -> int:
        host = cloud.GATEWAY_HOSTS[credentials.region]
        headers = cloud.request_headers(
            credentials.country,
            credentials.device_model,
            credentials.android_version,
            credentials.language,
        )
        try:
            login, _ = cloud.get_json(
                host,
                "/v4/users/login",
                cloud.login_params(
                    credentials.region,
                    credentials.account,
                    credentials.password,
                    credentials.device_brand,
                    credentials.device_model,
                    credentials.android_version,
                ),
                headers,
                timeout,
                auth_request=True,
            )
            login_data = login.get("data")
            if not isinstance(login_data, dict):
                raise YiAccountCredentialError("unexpected_response_schema", "YI returned an unexpected login response.")
            user_id = cloud._user_id(login_data.get("userid"))
            token = login_data.get("token")
            token_secret = login_data.get("token_secret")
            if user_id is None or not isinstance(token, str) or not token or not isinstance(token_secret, str) or not token_secret:
                raise YiAccountCredentialError("unexpected_response_schema", "YI returned an incomplete login response.")

            devices, _ = cloud.get_json(
                host,
                "/v4/devices/list",
                cloud.device_list_params(user_id, token, token_secret),
                headers,
                timeout,
            )
            cameras = devices.get("data")
            if not isinstance(cameras, list) or not all(isinstance(item, dict) for item in cameras):
                raise YiAccountCredentialError("unexpected_response_schema", "YI returned an unexpected camera inventory response.")
            return len(cameras)
        except cloud.YiCloudError as exc:
            raise YiAccountCredentialError(exc.category, exc.safe_message) from exc

    def _persist(self, credentials: YiAccountCredentials) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass

        # yi_tnp_oracle.load_env_file intentionally implements a very small
        # dotenv grammar. Inputs above reject newlines, surrounding whitespace
        # and matching wrapper quotes so raw KEY=value lines round-trip exactly.
        lines = [f"{key}={value}" for key, value in credentials.env().items()]
        payload = "\n".join(lines) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent, text=True)
        tmp = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
            try:
                dir_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    @staticmethod
    def _apply(credentials: YiAccountCredentials) -> None:
        for key, value in credentials.env().items():
            os.environ[key] = value

    def configure(
        self,
        payload: Mapping[str, Any],
        *,
        timeout: float = 10.0,
        cloud_session: "YiCloudSession | None" = None,
    ) -> dict[str, Any]:
        credentials = YiAccountCredentials.from_payload(payload)
        if cloud_session is None:
            camera_count = self._validate_live(credentials, timeout)
            self._persist(credentials)
            self._apply(credentials)
        else:
            def commit() -> None:
                self._persist(credentials)
                self._apply(credentials)

            try:
                camera_count = cloud_session.replace_credentials(credentials.env(), commit)
            except cloud.YiCloudError as exc:
                raise YiAccountCredentialError(exc.category, exc.safe_message) from exc
            except RuntimeError as exc:
                raise YiAccountCredentialError(
                    "unexpected_response_schema",
                    "YI returned an incomplete account or camera response.",
                ) from exc
        return {
            "ok": True,
            **credentials.safe_status(camera_count=camera_count),
            "credentials_persisted": True,
            "credential_file_mode": "0600",
        }
