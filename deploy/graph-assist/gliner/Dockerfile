FROM vllm/vllm-openai:bge-m3-fast-20260731

USER root
RUN python -m pip install --no-cache-dir gliner==0.2.28

WORKDIR /service
COPY server.py /service/server.py

ENV GLINER_MODEL=urchade/gliner_multi-v2.1 \
    GLINER_REVISION=443d26d654e0324125a96bebd8e796c14ff2efe6 \
    GLINER_MAX_LENGTH=1024 \
    GLINER_MAX_BATCH_SIZE=32

EXPOSE 8000
ENTRYPOINT ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
