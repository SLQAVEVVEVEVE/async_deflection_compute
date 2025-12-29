from __future__ import annotations

from concurrent import futures
import os
import random
import time
from typing import Any

import requests
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


MAIN_SERVICE_BASE_URL = os.environ.get("MAIN_SERVICE_BASE_URL", "https://localhost:3000").rstrip("/")
MAIN_SERVICE_ASYNC_RESULT_PATH = os.environ.get(
    "MAIN_SERVICE_ASYNC_RESULT_PATH",
    "/api/beam_deflections/{beam_deflection_id}/async_result",
)

CALLBACK_AUTH_TOKEN = os.environ.get("CALLBACK_AUTH_TOKEN", "12345678")
CALLBACK_AUTH_HEADER = os.environ.get("CALLBACK_AUTH_HEADER", "X-Async-Token")
CALLBACK_AUTH_SCHEME = os.environ.get("CALLBACK_AUTH_SCHEME", "")

MAIN_SERVICE_TIMEOUT_SECONDS = _env_int("MAIN_SERVICE_TIMEOUT_SECONDS", 10)
# Dev-default is False because Rails uses a self-signed cert in this lab setup.
MAIN_SERVICE_VERIFY_TLS = _env_bool("MAIN_SERVICE_VERIFY_TLS", False)

DELAY_MIN_SECONDS = _env_int("ASYNC_DELAY_MIN_SECONDS", 5)
DELAY_MAX_SECONDS = _env_int("ASYNC_DELAY_MAX_SECONDS", 10)
MAX_WORKERS = _env_int("ASYNC_MAX_WORKERS", 5)

executor = futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)


def _build_main_service_url(path_template: str, **params: object) -> str:
    path = path_template.format(**params)
    if not path.startswith("/"):
        path = "/" + path
    return MAIN_SERVICE_BASE_URL + path


def _build_auth_headers(token_override: str | None = None) -> dict[str, str]:
    token = token_override if token_override is not None else CALLBACK_AUTH_TOKEN
    token = str(token).strip()
    if CALLBACK_AUTH_SCHEME and " " not in token:
        token = f"{CALLBACK_AUTH_SCHEME} {token}"
    return {CALLBACK_AUTH_HEADER: token, "Content-Type": "application/json"}


def _to_float(value: object) -> float:
    if value is None:
        raise ValueError("value is None")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value.strip().replace(",", "."))
    raise ValueError("unsupported type")


def _to_int(value: object) -> int:
    if value is None:
        raise ValueError("value is None")
    if isinstance(value, bool):
        raise ValueError("unsupported type")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value.strip())
    raise ValueError("unsupported type")


def calculate_deflection_mm(length_m: float, udl_kn_m: float, elasticity_gpa: float, inertia_cm4: float) -> float:
    """
    Прогиб балки при равномерно распределённой нагрузке.

    Формула (простой вариант): δ = 5 w L^4 / (384 E I)
    - w: Н/м (из кН/м)
    - L: м
    - E: Па (из ГПа)
    - I: м^4 (из см^4)
    """
    if length_m <= 0:
        raise ValueError("length_m must be > 0")
    if elasticity_gpa <= 0:
        raise ValueError("elasticity_gpa must be > 0")
    if inertia_cm4 <= 0:
        raise ValueError("inertia_cm4 must be > 0")

    w_n_per_m = udl_kn_m * 1000.0
    e_pa = elasticity_gpa * 1_000_000_000.0
    i_m4 = inertia_cm4 * 1e-8
    delta_m = (5.0 * w_n_per_m * (length_m**4)) / (384.0 * e_pa * i_m4)
    return delta_m * 1000.0


def send_async_result(payload: dict[str, Any], callback_url: str | None = None, callback_token: str | None = None) -> None:
    beam_deflection_id = payload.get("beam_deflection_id")
    try:
        url = (
            callback_url
            if callback_url
            else _build_main_service_url(
                MAIN_SERVICE_ASYNC_RESULT_PATH,
                beam_deflection_id=beam_deflection_id,
            )
        )
        response = requests.post(
            url,
            json=payload,
            headers=_build_auth_headers(callback_token),
            timeout=MAIN_SERVICE_TIMEOUT_SECONDS,
            verify=MAIN_SERVICE_VERIFY_TLS,
        )
        print(f"callback beam_deflection_id={beam_deflection_id}: {response.status_code} {response.text}")
    except Exception as e:
        print(f"callback error beam_deflection_id={beam_deflection_id}: {e}")


def calculate_deflection_job(
    beam_deflection_id: int,
    items: list[dict[str, Any]],
    callback_url: str | None = None,
    callback_token: str | None = None,
) -> None:
    delay_min = max(0, min(DELAY_MIN_SECONDS, DELAY_MAX_SECONDS))
    delay_max = max(0, max(DELAY_MIN_SECONDS, DELAY_MAX_SECONDS))
    calculation_time = random.randint(delay_min, delay_max)

    print(
        f"start async deflection beam_deflection_id={beam_deflection_id} "
        f"items={len(items)} delay={calculation_time}s"
    )
    time.sleep(calculation_time)

    results: list[dict[str, Any]] = []
    total_qty = 0
    weighted_sum = 0.0
    within_norm = True

    for item in items:
        try:
            beam_id = _to_int(item["beam_id"])
            quantity = max(0, _to_int(item.get("quantity", 1)))
            length_m = _to_float(item["length_m"])
            udl_kn_m = _to_float(item["udl_kn_m"])

            beam = item.get("beam") or {}
            elasticity_gpa = _to_float(beam["elasticity_gpa"])
            inertia_cm4 = _to_float(beam["inertia_cm4"])
            allowed_ratio = _to_int(beam.get("allowed_deflection_ratio", 0))

            deflection_mm = calculate_deflection_mm(
                length_m=length_m,
                udl_kn_m=udl_kn_m,
                elasticity_gpa=elasticity_gpa,
                inertia_cm4=inertia_cm4,
            )
            deflection_mm = round(deflection_mm, 6)

            if allowed_ratio > 0:
                allowed_mm = (length_m * 1000.0) / allowed_ratio
                within_norm = within_norm and (deflection_mm <= allowed_mm)
            else:
                within_norm = False

            total_qty += quantity
            weighted_sum += deflection_mm * quantity

            results.append({"beam_id": beam_id, "deflection_mm": deflection_mm})
        except Exception as e:
            print(f"item error beam_deflection_id={beam_deflection_id}: {e}; item={item}")
            within_norm = False

    # Align with Rails: total deflection = sum(per-item deflection * quantity).
    result_deflection_mm = round(weighted_sum, 6)

    send_async_result(
        {
            "beam_deflection_id": beam_deflection_id,
            "calculated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "within_norm": within_norm,
            "result_deflection_mm": result_deflection_mm,
            "items": results,
        },
        callback_url=callback_url,
        callback_token=callback_token,
    )


@api_view(["POST"])
def calculate_deflection(request):
    if "beam_deflection_id" not in request.data or "items" not in request.data:
        return Response(
            {"error": "Ожидаются поля beam_deflection_id и items[]"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        beam_deflection_id = _to_int(request.data["beam_deflection_id"])
    except ValueError:
        return Response(
            {"error": "beam_deflection_id должен быть числом"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    items = request.data["items"]
    if not isinstance(items, list) or len(items) == 0:
        return Response(
            {"error": "items должен быть непустым массивом"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    callback_url: str | None = None
    callback_token: str | None = None
    callback = request.data.get("callback")
    if isinstance(callback, dict):
        raw_url = callback.get("url")
        if isinstance(raw_url, str) and raw_url.strip():
            callback_url = raw_url.strip()
        raw_token = callback.get("token")
        if raw_token is not None and str(raw_token).strip():
            callback_token = str(raw_token).strip()

    executor.submit(calculate_deflection_job, beam_deflection_id, items, callback_url, callback_token)

    return Response(
        {
            "message": "Async-расчёт прогиба запущен",
            "beam_deflection_id": beam_deflection_id,
            "items_count": len(items),
            "estimated_time": f"{min(DELAY_MIN_SECONDS, DELAY_MAX_SECONDS)}-{max(DELAY_MIN_SECONDS, DELAY_MAX_SECONDS)} секунд",
        },
        status=status.HTTP_202_ACCEPTED,
    )


@api_view(["GET"])
def health_check(request):
    return Response({"status": "healthy", "service": "async-deflection-calculator"}, status=status.HTTP_200_OK)
