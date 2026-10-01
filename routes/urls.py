from django.urls import path

from . import views

urlpatterns = [
    path("routes/optimize/", views.OptimizeRouteView.as_view(), name="optimize-route"),
]
