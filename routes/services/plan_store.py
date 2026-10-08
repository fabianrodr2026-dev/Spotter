from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from django.conf import settings
from django.core.cache import cache


PLAN_ID_PREFIX = "plan-id:"
PLAN_MATERIAL_PREFIX = "plan-material:"


def material_cache_key(material: dict[str, Any]) -> str:
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"{PLAN_MATERIAL_PREFIX}{digest}"


def plan_id_cache_key(plan_id: str) -> str:
    return f"{PLAN_ID_PREFIX}{plan_id}"


def new_plan_id() -> str:
    return uuid.uuid4().hex


def get_cached_plan_by_material(material: dict[str, Any]) -> dict[str, Any] | None:
    payload = cache.get(material_cache_key(material))
    return payload if isinstance(payload, dict) else None


def get_cached_plan_by_id(plan_id: str) -> dict[str, Any] | None:
    if not plan_id or not isinstance(plan_id, str):
        return None
    payload = cache.get(plan_id_cache_key(plan_id))
    return payload if isinstance(payload, dict) else None


def store_successful_plan(plan_id: str, material: dict[str, Any], payload: dict[str, Any]) -> None:
    timeout = int(settings.PLAN_CACHE_TIMEOUT_SECONDS)
    cache.set(material_cache_key(material), payload, timeout=timeout)
    cache.set(plan_id_cache_key(plan_id), payload, timeout=timeout)
