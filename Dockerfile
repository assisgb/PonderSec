FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    git \
    && rm -rf /var/lib/apt/lists/*
# Define o diretório de trabalho dentro do contêiner
WORKDIR /app

RUN pip install --upgrade pip

RUN pip install git+https://github.com/assisgb/open-cha-cybersec-version.git
# Copia os arquivos de requisitos para o contêiner
COPY requirements.txt .

# Instala as dependências do projeto
RUN pip install --no-cache-dir -r requirements.txt  

# Copia todo o código do projeto para o contêiner
COPY . .            
# Expõe a porta que o Django usará
EXPOSE 8000

# Comando para iniciar o servidor Django escutando em todos os IPs (0.0.0.0)
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]