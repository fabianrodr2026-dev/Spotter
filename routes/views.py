from __future__ import annotations

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .response import error_response
from .services.plan_api import PlanRequestError, plan_route_payload, status_for_error
from .services.plan_store import get_cached_plan_by_id


@require_GET
def health(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


@csrf_exempt
@require_POST
def plan_route(request: HttpRequest) -> JsonResponse:
    if request.content_type != "application/json":
        return error_response("invalid_input", "Content-Type must be application/json", 400)
    try:
        payload, status = plan_route_payload(request.body)
    except PlanRequestError as exc:
        return error_response(exc.code, exc.message, status_for_error(exc.code))
    return JsonResponse(payload, status=status)


@require_GET
def map_page(request: HttpRequest, plan_id: str | None = None) -> HttpResponse:
    if not plan_id:
        return render(
            request,
            "routes/map.html",
            {
                "map_state": "missing",
                "plan": None,
                "error_message": "Open a map URL returned by a successful plan response.",
            },
        )
    plan = get_cached_plan_by_id(plan_id)
    if plan is None:
        return render(
            request,
            "routes/map.html",
            {
                "map_state": "expired",
                "plan": None,
                "error_message": "This map link is unknown or has expired. Request a new plan.",
            },
            status=404,
        )
    return render(
        request,
        "routes/map.html",
        {
            "map_state": "ready",
            "plan": plan,
            "error_message": "",
        },
    )
