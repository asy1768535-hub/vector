"""将已有的 Qdrant collection case_chunks_000 注册为可检索的库。

只需运行一次。脚本直接写 sys_libraries，跳过 Qdrant collection 创建（collection 已存在）。

用法：
    cd D:/work_space/vectorDatabase
    python scripts/register_case_chunks.py

可选参数（覆盖默认值）：
    --slug          库唯一标识，也是 Dify knowledge_id（默认 case-chunks）
    --name          显示名称（默认 案件知识库）
    --collection    Qdrant collection 名（默认 case_chunks_000）
    源库正文补全相关：--source-db / --source-table / --source-key-field /
    --source-key-column / --source-text-column / --source-key-type / --no-source
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

# 确保项目根在 PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2  # 同步驱动（bootstrap 风格，简单直接）
from psycopg2.extras import Json

from app.config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Register an existing Qdrant collection as a library.")
    parser.add_argument("--slug",       default="case-chunks",      help="URL-safe 唯一标识 (default: case-chunks)")
    parser.add_argument("--name",       default="案件知识库",         help="显示名称 (default: 案件知识库)")
    parser.add_argument("--collection", default="case_chunks_000",   help="Qdrant collection 名 (default: case_chunks_000)")
    parser.add_argument("--embedding-model",   default="bge-m3",     dest="embedding_model")
    parser.add_argument("--embedding-dim",     default=1024, type=int, dest="embedding_dim")
    parser.add_argument("--embedding-url",     default=None,         dest="embedding_url",
                        help="库级 embedding URL；留空则使用全局 .env 里的 EMBEDDING_BASE_URL")
    parser.add_argument("--chunk-size",        default=1200, type=int, dest="chunk_size")
    parser.add_argument("--chunk-overlap",     default=120,  type=int, dest="chunk_overlap")
    # 源库正文补全：Qdrant payload 只有 {case_id, section_id}，正文在 cpwsdata.case_full_texts
    parser.add_argument("--source-db",     default="cpwsdata",        dest="source_db",
                        help="源库名（同机），存放正文的库 (default: cpwsdata)")
    parser.add_argument("--source-table",  default="case_full_texts", dest="source_table",
                        help="源表 (default: case_full_texts)")
    parser.add_argument("--source-key-field",  default="case_id",     dest="source_key_field",
                        help="Qdrant payload 里的外键字段 (default: case_id)")
    parser.add_argument("--source-key-column", default="case_id",     dest="source_key_column",
                        help="源表用于匹配的列 (default: case_id)")
    parser.add_argument("--source-text-column", default="full_text",  dest="source_text_column",
                        help="源表里作为正文返回的列 (default: full_text)")
    parser.add_argument("--source-key-type",   default="bigint",      dest="source_key_type",
                        help="外键 PG 类型 (default: bigint)")
    parser.add_argument("--no-source", action="store_true", dest="no_source",
                        help="不配置源库补全（仅按 payload.text 返回）")
    args = parser.parse_args()

    # 组装 source_config（除非 --no-source）
    source_config = None
    if not args.no_source:
        source_config = {
            "db_name": args.source_db,
            "table": args.source_table,
            "key_field": args.source_key_field,
            "key_column": args.source_key_column,
            "text_column": args.source_text_column,
            "key_type": args.source_key_type,
        }
        # 与 admin API 一致：写库前先校验，避免存进一个检索时才报错的非法配置
        from app.services import source_enrichment
        try:
            source_enrichment.parse_source_config(source_config)
        except source_enrichment.SourceConfigError as exc:
            print(f"[ERROR] invalid source_config: {exc}")
            sys.exit(1)

    # ── 检查 Qdrant collection 是否真的存在 ──────────────────────────
    import httpx
    qdrant_url = f"{settings.qdrant_url.rstrip('/')}/collections/{args.collection}"
    try:
        resp = httpx.get(qdrant_url, timeout=5)
        if resp.status_code == 200:
            info = resp.json().get("result", {})
            vecs = info.get("config", {}).get("params", {}).get("vectors", {})
            size = vecs.get("size") if isinstance(vecs, dict) else None
            print(f"[OK] Qdrant collection '{args.collection}' exists (dim={size or 'unknown'})")
            if size and size != args.embedding_dim:
                print(f"[WARN] collection dim={size} != --embedding-dim {args.embedding_dim}, "
                      f"please verify")
        elif resp.status_code == 404:
            print(f"[ERROR] Qdrant collection '{args.collection}' not found!")
            sys.exit(1)
        else:
            print(f"[WARN] Qdrant returned {resp.status_code}, continuing registration")
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Cannot reach Qdrant ({exc}), continuing registration")

    # ── 写入 sys_libraries ────────────────────────────────────────────
    conn = psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        dbname=settings.db_name,
    )
    try:
        with conn:
            with conn.cursor() as cur:
                # 检查是否已注册
                cur.execute("SELECT id, slug FROM sys_libraries WHERE slug = %s", (args.slug,))
                existing = cur.fetchone()
                if existing:
                    # 已存在 → 只更新 source_config（让补全配置生效），其余字段保持
                    cur.execute(
                        # 接入已有外部 collection → 必须 external（其生命周期由外部系统管理，
                        # 本系统不写/不 rebuild，且不参与 revision/tombstone 过滤，#6 §4.5）
                        "UPDATE sys_libraries SET source_config = %s, lifecycle_mode = 'external' WHERE slug = %s",
                        (Json(source_config) if source_config else None, args.slug),
                    )
                    print(f"[INFO] slug='{args.slug}' already exists (id={existing[0]}).")
                    print(f"       Updated source_config -> {json.dumps(source_config, ensure_ascii=False) if source_config else 'NULL'}")
                    print("       lifecycle_mode -> external")
                    return

                lib_id = uuid.uuid4()
                cur.execute(
                    """
                    INSERT INTO sys_libraries
                        (id, slug, name, description,
                         embedding_model, embedding_dim, vector_distance,
                         embedding_base_url, chunk_size, chunk_overlap,
                         qdrant_collection, source_config, lifecycle_mode,
                         created_by, created_at, deleted_at)
                    VALUES
                        (%s, %s, %s, %s,
                         %s, %s, 'cosine',
                         %s, %s, %s,
                         %s, %s, 'external',
                         NULL, NOW(), NULL)
                    """,
                    (
                        str(lib_id),
                        args.slug,
                        args.name,
                        f"接入已有 Qdrant collection：{args.collection}",
                        args.embedding_model,
                        args.embedding_dim,
                        args.embedding_url,   # NULL → 使用全局
                        args.chunk_size,
                        args.chunk_overlap,
                        args.collection,
                        Json(source_config) if source_config else None,
                    ),
                )
        print("[OK] Registration successful!")
        print(f"   id         : {lib_id}")
        print(f"   slug       : {args.slug}")
        print(f"   collection : {args.collection}")
        if source_config:
            print(f"   source     : {source_config['db_name']}.{source_config['table']} "
                  f"({source_config['key_field']} -> {source_config['text_column']})")
        else:
            print("   source     : (none, returns payload.text directly)")
        print()
        print("Next steps:")
        print("  1. Grant 'read' permission to the target user in admin UI -> Permissions")
        print(f"  2. POST /retrieval  body: {{\"knowledge_id\": \"{args.slug}\", \"query\": \"...\"}}")
        print(f"  3. OR   POST /libraries/{args.slug}/query  body: {{\"query\": \"...\", \"limit\": 5}}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
