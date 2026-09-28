#!/usr/bin/env bash
# Build a two-file overlay, replace only the API container, and retain the old container for rollback.
set -euo pipefail

release_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
container=vector-kb-api-release
base_image=vector-kb-app:error-messages-ui-20260923-r1
base_image_id=sha256:fa33261ff8d472c755a47c1bcf993bec6867fc28902b3fae12c7575bf0116ed8
new_image=vector-kb-app:error-messages-ui-20260923-r2
suffix=error-messages-ui-20260923-r2
rollback="${container}-rollback-${suffix}"

[[ "$(docker inspect --format '{{.Image}}' "$container")" == "$base_image_id" ]]
[[ "$(docker image inspect --format '{{.Id}}' "$base_image")" == "$base_image_id" ]]
if docker inspect "$rollback" >/dev/null 2>&1; then
    echo "Rollback slot is already occupied" >&2
    exit 1
fi
if docker image inspect "$new_image" >/dev/null 2>&1; then
    echo "Release image tag is already occupied" >&2
    exit 1
fi

docker build --pull=false --build-arg "BASE_IMAGE=$base_image" \
    --tag "$new_image" --file "$release_dir/Dockerfile.error-messages-ui-r2" "$release_dir"

docker run --rm --entrypoint python "$new_image" -c '
from app.services.import_uploads import _personal_failure_message
message, action = _personal_failure_message(status="failed", stage="parsing", raw_error="no_slug")
assert message == "该任务缺少解析所需的知识库信息"
assert action == "重新上传该文件，生成新的解析任务"
message, action = _personal_failure_message(status="failed", stage="parsing", raw_error="ForeignKeyViolationError: fk_document_import_jobs_file_resource")
assert message == "该任务未能关联到原文件记录"
assert action == "重新上传该文件，生成新的处理任务"
print("failure guidance smoke: ok")
'
docker run --rm --entrypoint python "$new_image" -c '
from pathlib import Path
source = Path("/app/admin-ui/src/views/MyTasks.js").read_text(encoding="utf-8")
assert "若仍失败，请联系管理员" in source
print("task UI smoke: ok")
'

[[ "$(docker inspect --format '{{.Image}}' "$container")" == "$base_image_id" ]]
swapped=0
rollback_on_failure() {
    status=$?
    trap - EXIT
    if [[ "$swapped" -eq 1 && "$status" -ne 0 ]]; then
        docker rm -f "$container" >/dev/null 2>&1 || true
        docker rename "$rollback" "$container"
        docker start "$container" >/dev/null
        echo "Release check failed; prior container restored" >&2
    fi
    exit "$status"
}
trap rollback_on_failure EXIT

bash "$release_dir/recreate_container_from_existing.sh" "$container" "$new_image" "$suffix"
swapped=1

ready=0
for attempt in $(seq 1 30); do
    if curl --fail --silent http://127.0.0.1:8200/health/ready >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done
[[ "$ready" -eq 1 ]]
curl --fail --silent http://127.0.0.1:8200/console/src/views/MyTasks.js | grep --fixed-strings '若仍失败，请联系管理员' >/dev/null
docker exec "$container" python -c 'from app.services.import_uploads import _personal_failure_message; assert _personal_failure_message(status="failed", stage="parsing", raw_error="no_slug")[0] == "该任务缺少解析所需的知识库信息"'

swapped=0
trap - EXIT
docker ps --filter "name=^/${container}$" --format 'Active: {{.Names}} {{.Image}} {{.Status}}'
echo "Release checks passed; prior container retained as $rollback"
