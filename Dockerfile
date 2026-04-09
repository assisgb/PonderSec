FROM python:3.11-slim


#SEÇÃO INICIAL
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
WORKDIR /app
COPY requirements.txt .
COPY . .


#SEÇÃO DE ATUALIZAÇÃO E INSTALAÇÃO
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    git \
    && rm -rf /var/lib/apt/lists/*


#SEÇÃO DE INSTALAÇÃO DE DEPENDÊNCIAS
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt  



#SEÇÃO DE SEGURANÇA
RUN addgroup --system appgroup && \
    adduser --system --ingroup appgroup appuser   
RUN chown -R appuser:appgroup /app
USER appuser


#EXPÕE A PORTA 8000
EXPOSE 8000


#COMANDO PARA INICIAR O SERVIDOR
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
