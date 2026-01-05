FROM python:3.13

# Define o diretório de trabalho dentro do contêiner
WORKDIR /app    

ENV PYTHONUNBUFFERED 1
ENV GEMINI_API_KEY="AIzaSyB7xddDQsOpU1j5yME2Svs-HXD3zJ0RTV8"
ENV GROQ_API_KEY="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"
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