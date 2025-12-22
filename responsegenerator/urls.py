from django.urls import path
from . import views

urlpatterns = [
    path('pondersecoptions/', views.pondersecoptions, name="pondersecoptions"),
    path('perguntar/', views.perguntar, name="perguntar"),
    path('historico/', views.historico, name="historico"),

]