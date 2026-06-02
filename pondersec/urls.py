from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('i18n/', include('django.conf.urls.i18n')),
    # Responsegenerator primeiro (para rota raiz ser o chat público)
    path('', include('responsegenerator.urls')),
    # Usuários com prefixo para evitar conflito
    path('auth/', include('usuarios.urls')),
]
