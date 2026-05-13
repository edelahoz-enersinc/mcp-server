FROM python:3.12-slim

# Evitar que Python genere archivos .pyc y que el log se quede en el buffer
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copiar el archivo de dependencias
COPY requirements.txt .

# Instalación estándar de dependencias (sin el --mount que dio error)
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto del código
COPY . .

# Exponer el puerto 8080
EXPOSE 8080

# Comando para iniciar la aplicación
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
