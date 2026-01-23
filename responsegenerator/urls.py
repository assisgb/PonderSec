from django.urls import path
from . import views

urlpatterns = [
    path('pondersecoptions/', views.pondersecoptions, name="pondersecoptions"),
    path('add_quest/', views.add_questao_options, name="add_quest"),
    path('consulta/', views.consulta, name="consulta"),
    path('historico/', views.historico, name="historico"),
    path('historico/deletar/<int:id>/', views.deletar_item_historico, name='deletar_item_historico'),
    path('historico/ver/<int:id>/', views.ver_detalhes, name='ver_detalhes'),
    path('historico/limpar/', views.limpar_historico, name='limpar_historico'),
]
