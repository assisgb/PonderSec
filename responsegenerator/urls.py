from django.urls import path
from . import views

urlpatterns = [
    path('pondersecoptions/', views.pondersecoptions, name="pondersecoptions"),
    path('perguntar/', views.perguntar, name="perguntar"),
    path('historico/', views.historico, name="historico"),
    path('historico/deletar/<int:id>/', views.deletar_item_historico, name='deletar_item_historico'),
    path('historico/ver/<int:id>/', views.ver_detalhes, name='ver_detalhes'),
    path('historico/limpar/', views.limpar_historico, name='limpar_historico'),
]
