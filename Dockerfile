FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt ruff \
    && python -m playwright install --with-deps chromium
COPY app app
COPY templates templates
COPY static static
COPY tests tests
COPY pytest.ini .
ENV AMBER_DATA_DIR=/tmp/amber-test-data
CMD ["python", "-m", "pytest"]
