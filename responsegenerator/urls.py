from django.urls import path
from . import views

# ======================================================================
# ROTAS PÚBLICAS (SEM LOGIN)
# ======================================================================
urlpatterns_publicas = [
    path('', views.usuario_final_chat, name='usuario_final_chat'),
    path('api/usuario-final-chat/', views.usuario_final_chat_api, name='usuario_final_chat_api'),
    path('avaliacao/responder/<int:formulario_id>/', views.responder_avaliacao_publica, name='responder_avaliacao_publica'),
]

# ======================================================================
# ROTAS DE QUESTÕES
# ======================================================================
urlpatterns_questoes = [
    path('questoes/', views.questoes, name='questoes'),
    path('questoes/add/', views.add_questoes, name='add_questoes'),
    path('questoes/upload/', views.upload_perguntas, name='upload_perguntas'),
    path('questoes/download-template/<str:formato>/', views.download_template_perguntas, name='download_template_perguntas'),
    path('questoes/cadastro-categoria/', views.questoes_cadastro_categoria, name='questoes_cadastro_categoria'),
    path('questoes/ver/<int:id>/', views.ver_detalhes_questao, name='ver_detalhes_questao'),
    path('questoes/<int:id>/adicionar-resposta-humana/', views.adicionar_resposta_humana_questao, name='adicionar_resposta_humana_questao'),
    path('questoes/resposta/<int:resposta_id>/deletar/', views.deletar_resposta_ia, name='deletar_resposta_ia'),
    path('questoes/<int:questao_id>/gerar-ia/<int:llm_id>/', views.gerar_resposta_ia_unica, name='gerar_resposta_ia_unica'),
    path('questoes/<int:questao_id>/gerar-ia-faltantes/', views.gerar_respostas_ia_faltantes, name='gerar_respostas_ia_faltantes'),
    path('questoes/deletar/<int:id>/', views.deletar_questao_historico, name='deletar_questao_historico'),
    path('questoes/limpar/', views.limpar_questoes, name='limpar_questoes'),
    path('categoria/editar/<int:id>/', views.editar_categoria, name='editar_categoria'),
]

# ======================================================================
# ROTAS DE CONSULTA
# ======================================================================
urlpatterns_consulta = [
    path('menu-consulta/', views.menu_consulta, name='menu_consulta'),
    path('executar-consulta/', views.executar_consulta, name='executar_consulta'),
    path('consulta-comparacao/', views.consulta_comparacao, name='consulta_comparacao'),
    path('respostas/<int:questao_id>/', views.get_respostas, name='get_respostas'),
    path('gerar-resposta/<int:questao_id>/', views.gerar_respostas, name='gerar_respostas'),
    path('limpar-respostas/', views.limpar_respostas, name='limpar_respostas'),
]

# ======================================================================
# ROTAS DE AVALIAÇÃO
# ======================================================================
urlpatterns_avaliacao = [
    path('avaliacao/', views.avaliacao, name='avaliacao'),
    path('avaliacao/adicionar/', views.avaliacao_adicionar_formulario, name='avaliacao_adicionar_formulario'),
    path('avaliacao/editar/<int:id>/', views.avaliacao_editar_formulario, name='avaliacao_editar_formulario'),
    path('avaliacao/deletar/<int:id>/', views.avaliacao_deletar_formulario, name='avaliacao_deletar_formulario'),
    path('avaliacao/<int:formulario_id>/questao/<int:questao_id>/', views.avaliacao_respostas, name='avaliacao_respostas'),
    path('avaliacao/dashboard/', views.dashboard_avaliacoes, name='dashboard_avaliacoes'),
    path('avaliacao/dashboard-comparativo/', views.dashboard_comparativo_avaliacoes, name='dashboard_comparativo_avaliacoes'),
    path('menu-avaliacao/', views.menu_avaliacao, name='menu_avaliacao'),
]

# ======================================================================
# ROTAS DE JUÍZES
# ======================================================================
urlpatterns_juizes = [
    path('juizes/comparador/', views.juizes_comparador, name='juizes_comparador'),
    path('juizes/avaliar/', views.juizes_executar_avaliacao, name='juizes_executar_avaliacao'),
]

# ======================================================================
# ROTAS DE SETUP (CONFIGURAÇÃO INTERNA)
# ======================================================================
urlpatterns_setup = [
    path('setup/', views.setup, name='setup'),
    path('setup/llm/', views.setup_llm, name='setup_llm'),
    path('setup/configurar-llm/', views.setup_configurar_llm, name='setup_configurar_llm'),
    path('setup/avaliacao/', views.setup_avaliacao, name='setup_avaliacao'),
    path('setup/adicionar-metrica/', views.setup_adicionar_metrica, name='setup_adicionar_metrica'),
    path('setup/configurar-metrica/', views.setup_configurar_metrica, name='setup_configurar_metrica'),
    path('setup/deletar-metrica/<int:id>/', views.setup_deletar_metrica, name='setup_deletar_metrica'),
    path('historico/', views.historico, name='historico'),
]

# ======================================================================
# ROTAS DE API (INTERNA)
# ======================================================================
urlpatterns_api = [
    path('api/llm/<int:id>/delete/', views.deletar_llm, name='delete_llm'),
    path('api/llm/<int:id>/edit/', views.edit_llm_api, name='edit_llm_api'),
]

# ======================================================================
# PAINEL ADMIN (auth separada via AdminPonderSec)
# ======================================================================
urlpatterns_admin = [
    path('admin-pondersec/login/', views.admin_pondersec_login, name='admin_pondersec_login'),
    path('admin-pondersec/logout/', views.admin_pondersec_logout, name='admin_pondersec_logout'),
    path('admin-pondersec/', views.admin_pondersec_home, name='admin_pondersec_home'),
    path('admin-pondersec/metricas-publicas/', views.admin_pondersec_metricas_publicas, name='admin_pondersec_metricas_publicas'),
    path('admin-pondersec/metricas-publicas/<int:id>/editar/', views.admin_pondersec_metrica_publica_editar, name='admin_pondersec_metrica_publica_editar'),
    path('admin-pondersec/metricas-publicas/<int:id>/deletar/', views.admin_pondersec_metrica_publica_deletar, name='admin_pondersec_metrica_publica_deletar'),
    path('admin-pondersec/metricas-publicas/<int:id>/toggle/', views.admin_pondersec_metrica_publica_toggle, name='admin_pondersec_metrica_publica_toggle'),
    path('admin-pondersec/avaliacoes-publicas/', views.admin_pondersec_avaliacoes_publicas, name='admin_pondersec_avaliacoes_publicas'),
    path('admin-pondersec/llms-publicas/', views.admin_pondersec_llms_publicas, name='admin_pondersec_llms_publicas'),
    path('admin-pondersec/llms-publicas/<int:id>/editar/', views.admin_pondersec_llm_publica_editar, name='admin_pondersec_llm_publica_editar'),
    path('admin-pondersec/llms-publicas/<int:id>/deletar/', views.admin_pondersec_llm_publica_deletar, name='admin_pondersec_llm_publica_deletar'),
    path('admin-pondersec/llms-publicas/<int:id>/toggle/', views.admin_pondersec_llm_publica_toggle, name='admin_pondersec_llm_publica_toggle'),
]

# ======================================================================
# MENU PRINCIPAL
# ======================================================================
urlpatterns_menu = [
    path('menu/', views.menu, name='menu'),
]

# ======================================================================
# CONCATENAÇÃO FINAL
# ======================================================================
urlpatterns = (
    urlpatterns_publicas
    + urlpatterns_menu
    + urlpatterns_questoes
    + urlpatterns_consulta
    + urlpatterns_avaliacao
    + urlpatterns_juizes
    + urlpatterns_setup
    + urlpatterns_api
    + urlpatterns_admin
)
