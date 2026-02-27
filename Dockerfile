FROM python:3.11

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir flask pytesseract pdf2image reportlab werkzeug

EXPOSE 5000

CMD ["python", "app.py"]