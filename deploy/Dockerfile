# syntax=docker/dockerfile:1

# Use the existing DOC-conversion runtime for every role. It is a superset of
# the normal runtime, so one application image retains both import and legacy
# Office conversion support.
ARG BASE_IMAGE=invalid.local/vector-kb-base-image-required:missing
FROM ${BASE_IMAGE}

ARG VCS_REF
LABEL org.opencontainers.image.title="vector-knowledge" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="compat-0062"

USER root
RUN test -n "${VCS_REF}" || (echo "VCS_REF is required" >&2; exit 1)

# The existing server image omitted two runtime dependencies declared by the
# application: legacy XLS parsing and RFC 8785 canonical JSON for graph lineage.
RUN python -m pip install --no-cache-dir "xlrd==2.0.2" "rfc8785==0.1.4"

# Replace the complete application tree so every role runs one code identity.
RUN rm -rf /app/app /app/admin-ui /app/alembic /app/scripts
COPY --chown=10001:10001 app/ /app/app/
COPY --chown=10001:10001 admin-ui/ /app/admin-ui/
COPY --chown=10001:10001 alembic/ /app/alembic/
COPY --chown=10001:10001 scripts/ /app/scripts/
COPY --chown=10001:10001 alembic.ini pyproject.toml /app/

# Build-time gate: legacy parsing and graph lineage imports must exist at runtime.
RUN python -c "import rfc8785, xlrd; import app.services.canonical_entity_evolution, app.services.folders, app.services.graph_extraction_jobs; print('application import smoke: ok')"

USER 10001
