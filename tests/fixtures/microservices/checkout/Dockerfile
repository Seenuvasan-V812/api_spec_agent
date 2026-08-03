FROM python:3.12-slim
COPY . /app
WORKDIR /app
RUN pip install -r requirements.txt
CMD ["uvicorn", "checkout.main:app", "--host", "0.0.0.0", "--port", "8000"]
