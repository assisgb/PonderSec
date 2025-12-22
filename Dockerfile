FROM python:3.13

# Define o diretório de trabalho dentro do contêiner
WORKDIR /app    

ENV PYTHONUNBUFFERED 1
ENV GEMINI_API_KEY="API_KEY"
ENV GROQ_API_KEY="API_KEY"
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