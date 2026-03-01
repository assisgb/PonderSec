from django.urls import path
from . import views

urlpatterns = [
    path('menu/', views.menu, name="menu"),
    path('questoes/', views.questoes, name="questoes"),
    path('add_questoes/', views.add_questoes, name="add_questoes"),
    path('questoes-upload/', views.questoes_upload, name="questoes_upload"),
    path('menu_consulta/', views.menu_consulta, name="menu-consulta"),
    path('consulta_comparacao/', views.consulta_comparacao, name="consulta_comparacao"),
    path('executar_consulta', views.executar_consulta, name="executar_consulta"),
    path('avaliacao/', views.avaliacao, name="avaliacao"),
    path('avaliacao_respostas/', views.avaliacao_respostas, name="avaliacao_respostas"),
    path('setup/', views.setup, name="setup"),
    path('setup_llm/', views.setup_llm, name="setup_llm"),
    path('setup_avaliacao/', views.setup_avaliacao, name="setup_avaliacao"),
    path('historico/', views.historico, name="historico"),
    path('historico/deletar/<int:id>/', views.deletar_item_historico, name='deletar_item_historico'),
    path('historico/ver/<int:id>/', views.ver_detalhes, name='ver_detalhes'),
    path('historico/limpar/', views.limpar_historico, name='limpar_historico'),
]
