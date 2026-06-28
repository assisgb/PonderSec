from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("i18n/", include("django.conf.urls.i18n")),
    path("auth/", include("usuarios.urls")),
    # Deve permanecer por último porque contém a rota raiz do chat público.
    path("", include("responsegenerator.urls")),
]
