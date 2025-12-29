from django.urls import path
from . import views

urlpatterns = [
    path('api/v1/calculate-deflection/', views.calculate_deflection, name='calculate-deflection'),
    path('api/health/', views.health_check, name='health-check'),
]
