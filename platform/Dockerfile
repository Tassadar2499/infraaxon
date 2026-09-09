FROM python:3.12.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY platform/infraaxon /app/infraaxon
RUN mkdir /data && chown 1000:1000 /data
USER 1000:1000
EXPOSE 8000
CMD ["uvicorn", "infraaxon.api:app", "--host", "0.0.0.0", "--port", "8000"]
