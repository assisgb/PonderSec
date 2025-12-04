from django.urls import path
from . import views

urlpatterns = [
    path('cadastro/', views.cadastro, name="cadastro"),
    path('', views.login, name="login"),
    path('tela-inicial/', views.tela_inicial, name="tela_inicial"),

]