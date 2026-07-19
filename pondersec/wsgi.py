"""
WSGI config for pondersec project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/wsgi/
"""

import os

from django.conf import settings
from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pondersec.settings')

application = get_wsgi_application()

# Com --preload, o Gunicorn executa isto uma única vez no processo mestre e os
# workers herdam os módulos prontos. Assim a primeira geração também não paga o
# custo de importar os SDKs, enquanto comandos de manutenção continuam leves.
if settings.LLM_PRELOAD_PROVIDER_SDKS:
    from responsegenerator.llm_client import preload_provider_dependencies

    preload_provider_dependencies()
