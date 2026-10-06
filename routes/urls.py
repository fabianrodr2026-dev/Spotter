from django.urls import path

from . import views


urlpatterns = [
    path("health/", views.health, name="health"),
    path("api/routes/plan/", views.plan_route, name="plan-route"),
    path("map/", views.map_page, name="map"),
]
