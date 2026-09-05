"""APK-derived YI cloud raw-model registry.

This module contains only non-secret metadata derived from YI Home
feature_config assets.  It intentionally preserves one-to-many server-model
mappings instead of guessing when the APK itself is ambiguous.

Source currently represented here:
  YI Home 5.6.5_20220819031350 (version code 319)
  SHA-256 776967311D74F2AC94FC91D3FA7AEDEA16B2023DAF9D14468931FA13EEF4F227

The cloud ``/v4/devices/list`` field named ``model`` is the raw/server model.
Firmware *version* is not supplied by that endpoint; ``firmware_branch`` below
comes from the APK feature config and is a model-family hint, not the running
camera firmware version.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCandidate:
    server_model: str
    model: str
    firmware_branch: str = ""
    identifier: str = ""
    base_version: int = 0
    config_version: int = 0
    platform: int = 0
    device_type: int = 0
    online_status_p2p: int = 0
    power_supply_type: int = 0
    h265_support: int = 0
    feature_config: str = ""


# Fields per tuple:
# model, firmware_branch, identifier, base_version, config_version, platform,
# device_type, online_status_p2p, power_supply_type, h265_support, feature_config
_RAW: dict[str, tuple[tuple[object, ...], ...]] = {
    "1": (("yunyi.camera.v1", "", "", 1, 1, 0, 1, 1, 0, 0, "config_h18y.json"),),
    "2": (("yunyi.camera.htwo1", "", "", 0, 0, 0, 1, 1, 0, 0, "config_h21.json"),),
    "3": (("h19", "", "", 1, 1, 0, 1, 1, 0, 0, "config_h19.json"),),
    "4": (("yunyi.camera.mj1", "", "", 0, 0, 0, 1, 1, 0, 0, "config_m20.json"),),
    "5": (("h20", "", "", 1, 1, 0, 1, 1, 0, 0, "config_h20.json"),),
    "6": (("yunyi.camera.y20", "", "", 1, 1, 0, 1, 1, 0, 0, "config_y20.json"),),
    "7": (("h30", "", "", 0, 0, 0, 1, 1, 0, 0, "config_h30.json"),),
    "12": (("y30", "familymonitor-y30", "", 0, 0, 1, 1, 1, 0, 0, "config_y30.json"),),
    "13": (("y31", "familymonitor-y31", "", 0, 0, 0, 1, 1, 0, 0, "config_y31.json"),),
    "14": (("y19", "familymonitor-y19", "", 0, 0, 0, 1, 1, 0, 0, "config_y19.json"),),
    "15": (("y25", "familymonitor-y25", "", 0, 0, 1, 1, 1, 0, 0, "config_y25.json"),),
    "17": (("y32", "familymonitor-y32", "", 0, 0, 1, 1, 1, 0, 0, "config_y32.json"),),
    "19": (("w10", "familymonitor-w10", "", 0, 0, 0, 3, 1, 1, 0, "config_w10.json"),),
    "20": (("n10", "familymonitor-n10", "", 0, 0, 0, 2, 1, 0, 0, "config_n10.json"),),
    "21": (("n20", "", "", 0, 0, 0, 4, 0, 2, 0, "config_n20.json"),),
    "22": (("n30", "", "", 0, 0, 0, 4, 0, 2, 0, "config_n30.json"),),
    "34": (("y501c", "familymonitor-y501gc", "", 0, 0, 1, 1, 1, 0, 0, "config_y501c.json"),),
    "35": (("h31", "familymonitor-h31", "", 0, 0, 1, 1, 1, 0, 1, "config_h31.json"),),
    "36": (("w102", "familymonitor-w102", "", 0, 0, 0, 3, 1, 1, 0, "config_w102.json"),),
    "38": (("y20ga", "familymonitor-y20ga", "", 1, 1, 0, 1, 1, 0, 0, "config_y20ga.json"),),
    "39": (("y25ga", "familymonitor-y25ga", "", 0, 0, 1, 1, 1, 0, 0, "config_y25ga.json"),),
    "40": (("y30ga", "familymonitor-y30qa", "", 0, 0, 1, 1, 1, 0, 0, "config_y30ga.json"),),
    "41": (("r30gb", "familymonitor-r30gb", "", 0, 0, 1, 1, 1, 0, 1, "config_r30gb.json"),),
    "43": (
        ("h31gc", "familymonitor-h31gc", "", 0, 0, 1, 1, 1, 0, 1, "config_h31gc.json"),
        ("y32gc", "familymonitor-y32gc", "", 0, 0, 1, 1, 1, 0, 0, "config_y32gc.json"),
    ),
    "46": (("y29ga", "familymonitor-y29ga", "", 1, 1, 0, 1, 1, 0, 0, "config_y29ga.json"),),
    "47": (("h50ga", "familymonitor-h50ga", "", 0, 0, 1, 1, 1, 0, 0, "config_h50ga.json"),),
    "50": (("y30gc", "familymonitor-y30gc", "", 0, 0, 1, 1, 1, 0, 0, "config_y30gc.json"),),
    "51": (("y21ga", "familymonitor-y21ga", "", 1, 1, 0, 1, 1, 0, 0, "config_y21ga.json"),),
    "52": (("h51ga", "familymonitor-h51ga", "", 0, 0, 1, 1, 1, 0, 0, "config_h51ga.json"),),
    "55": (("y28ga", "familymonitor-y28ga", "", 1, 1, 0, 1, 1, 0, 0, "config_y28ga.json"),),
    "56": (("h52ga", "familymonitor-h52ga", "", 0, 0, 1, 1, 1, 0, 0, "config_h52ga.json"),),
    "57": (("y502c", "familymonitor-y502gc", "", 0, 0, 1, 1, 1, 0, 0, "config_y502c.json"),),
    "60": (("h53ga", "familymonitor-h53ga", "", 0, 0, 1, 1, 1, 0, 0, "config_h53ga.json"),),
    "61": (("d201", "familymonitor-d201", "", 0, 0, 0, 1, 1, 1, 1, "config_d201.json"),),
    "62": (("h30ga", "familymonitor-h30ga", "", 0, 0, 0, 1, 1, 0, 0, "config_h30ga.json"),),
    "63": (("h31ga", "familymonitor-h31ga", "", 0, 0, 1, 1, 1, 0, 1, "config_h31ga.json"),),
    "64": (
        ("w102", "familymonitor-w12ga", "", 0, 0, 0, 3, 1, 1, 0, "config_W12GAModel.json"),
        ("w12ga", "familymonitor-w12ga", "", 0, 0, 0, 3, 1, 1, 0, "config_w12ga.json"),
    ),
    "65": (
        ("w102", "familymonitor-w102s", "", 0, 0, 0, 3, 1, 1, 0, "config_W102SModel.json"),
        ("w102s", "familymonitor-w102s", "", 0, 0, 0, 3, 1, 1, 0, "config_w102s.json"),
    ),
    "66": (("y281ga", "familymonitor-y281ga", "", 1, 1, 0, 1, 1, 0, 0, "config_y281ga.json"),),
    "67": (("y211ga", "familymonitor-y211ga", "", 1, 1, 0, 1, 1, 0, 0, "config_y211ga.json"),),
    "68": (("h60ga", "familymonitor-h60ga", "", 0, 0, 1, 1, 1, 0, 1, "config_h60ga.json"),),
    "69": (("h32ga", "familymonitor-h32ga", "", 0, 0, 0, 1, 1, 0, 0, "config_h32ga.json"),),
    "70": (("r40ga", "familymonitor-r40ga", "", 0, 0, 1, 1, 1, 0, 1, "config_r40ga.json"),),
    "71": (("r35gb", "familymonitor-r35gb", "", 0, 0, 1, 1, 1, 0, 1, "config_r35gb.json"),),
    "72": (("y26ga", "familymonitor-y26ga", "", 0, 0, 1, 1, 1, 0, 0, "config_y26ga.json"),),
    "76": (("b621", "familymonitor-b621", "", 0, 0, 1, 1, 1, 0, 1, "config_b621.json"),),
    "83": (("y291ga", "familymonitor-y291ga", "", 1, 1, 0, 1, 1, 0, 0, "config_y291ga.json"),),
    "84": (("y311ga", "familymonitor-y311ga", "", 1, 1, 0, 1, 1, 0, 0, "config_y311ga.json"),),
    "85": (("r36ga", "familymonitor-r36ga", "", 0, 0, 1, 1, 1, 0, 1, "config_r36ga.json"),),
    "10000": (
        ("iotk2", "familymonitor-h31gc", "A0016", 0, 0, 1, 1, 1, 0, 0, "config_iotk2.json"),
        ("iotv3", "familymonitor-h31gc", "A0010", 0, 0, 1, 1, 1, 0, 0, "config_iotv3.json"),
    ),
}


def candidates(raw_model: object, did: object = None) -> tuple[ModelCandidate, ...]:
    server_model = "" if raw_model is None else str(raw_model)
    rows = _RAW.get(server_model, ())
    result = tuple(ModelCandidate(server_model, *row) for row in rows)
    if server_model == "10000":
        device_id = "" if did is None else str(did)
        prefix = device_id[:5] if len(device_id) >= 5 else ""
        if prefix:
            matched = tuple(item for item in result if item.identifier == prefix)
            if matched:
                return matched
    return result


def resolve(raw_model: object, did: object = None) -> tuple[str, str]:
    """Return ``(model, evidence)`` without guessing ambiguous mappings."""
    found = candidates(raw_model, did)
    models = sorted({item.model for item in found})
    if len(models) == 1:
        evidence = "registry_identifier" if str(raw_model) == "10000" and found[0].identifier else "registry_unique"
        return models[0], evidence
    if len(models) > 1:
        return "UNKNOWN", "registry_ambiguous"
    return "UNKNOWN", "registry_missing"


def ambiguous_server_models() -> tuple[str, ...]:
    values = []
    for raw_model, rows in _RAW.items():
        if len({str(row[0]) for row in rows}) > 1:
            values.append(raw_model)
    return tuple(sorted(values, key=lambda value: int(value) if value.isdigit() else 1 << 30))


def all_server_models() -> tuple[str, ...]:
    return tuple(sorted(_RAW, key=lambda value: int(value) if value.isdigit() else 1 << 30))
