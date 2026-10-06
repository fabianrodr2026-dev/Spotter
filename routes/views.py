from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from .response import error_response
from .validation import InvalidPlanInput, parse_plan_input


@require_GET
def health(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


@require_POST
def plan_route(request: HttpRequest) -> JsonResponse:
    if request.content_type != "application/json":
        return error_response("invalid_input", "Content-Type must be application/json", 400)
    try:
        parse_plan_input(request.body)
    except InvalidPlanInput as exc:
        return error_response("invalid_input", str(exc), 400)
    return error_response("not_implemented", "Route planning is not available yet", 501)


@require_GET
def map_page(request: HttpRequest) -> HttpResponse:
    return render(request, "routes/map.html")
