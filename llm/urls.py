from django.urls import path
from .views import llm_home

urlpatterns = [
    path("", llm_home, name="llm_home"),
]
