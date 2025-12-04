from django.urls import path
from . import views

urlpatterns = [

    path('tela-inicial/', views.tela_inicial, name="tela_inicial"),

]