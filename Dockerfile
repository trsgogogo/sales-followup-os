FROM python:3.12-slim
WORKDIR /app
RUN useradd --uid 10001 --create-home app && mkdir /app/data && chown app:app /app/data
COPY --chown=app:app followup /app/followup
USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["python", "-m", "followup.server", "--host", "0.0.0.0"]
