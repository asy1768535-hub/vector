"""Correct the published P3.1 audit schema without rewriting revision 0071.

The migration deliberately has no Python-side cardinality branch.  Both online
and ``--sql`` output execute the same PostgreSQL lock and empty-table gate.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0072"
down_revision: str | Sequence[str] | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVOLUTION_TABLES = (
    "canonical_entity_evolution_commands",
    "canonical_entity_evolution_decisions",
    "canonical_entity_evolution_sources",
    "canonical_entity_evolution_successors",
    "canonical_entity_projection_assignments",
)
_MAINTENANCE_TABLES = (
    "sys_libraries",
    "canonical_entities",
    "entities",
    "entity_resolution_decisions",
    *_EVOLUTION_TABLES,
)


def _maintenance_lock_and_empty_gate(error_code: str) -> None:
    """Emit the same transactional preflight in connected and offline modes."""

    for table in _MAINTENANCE_TABLES:
        op.execute(
            f"""
            DO $$
            BEGIN
                LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE NOWAIT;
            EXCEPTION WHEN lock_not_available THEN
                RAISE EXCEPTION 'P3_1_0072_MAINTENANCE_LOCK_UNAVAILABLE';
            END $$;
            """
        )
    predicates = " OR ".join(f"EXISTS (SELECT 1 FROM {table})" for table in _EVOLUTION_TABLES)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF {predicates} THEN
                RAISE EXCEPTION '{error_code}';
            END IF;
        END $$;
        """
    )


def _drop_0071_schema() -> None:
    for table in reversed(_EVOLUTION_TABLES):
        op.execute(f"DROP TABLE {table}")


def _create_target_schema() -> None:
    op.execute(
        """
        CREATE TABLE canonical_entity_evolution_commands (
            id UUID PRIMARY KEY,
            library_id UUID NOT NULL REFERENCES sys_libraries(id) ON DELETE RESTRICT,
            idempotency_key VARCHAR(256) NOT NULL,
            command_identity_fingerprint VARCHAR(64) NOT NULL,
            operation_kind VARCHAR(32) NOT NULL,
            source_identity_snapshot JSONB NOT NULL,
            command_scope_snapshot JSONB NOT NULL,
            contract_version VARCHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_canonical_entity_evolution_commands_operation
                CHECK (operation_kind IN ('merge','split','reassign')),
            CONSTRAINT ck_canonical_entity_evolution_commands_identity_fingerprint
                CHECK (command_identity_fingerprint ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_canonical_entity_evolution_commands_snapshots
                CHECK (jsonb_typeof(source_identity_snapshot) = 'object'
                   AND jsonb_typeof(command_scope_snapshot) = 'object'),
            CONSTRAINT uq_canonical_entity_evolution_commands_id_library UNIQUE (id, library_id),
            CONSTRAINT uq_canonical_entity_evolution_commands_idempotency UNIQUE (library_id, idempotency_key),
            CONSTRAINT uq_canonical_entity_evolution_commands_identity UNIQUE (library_id, command_identity_fingerprint)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE canonical_entity_evolution_decisions (
            id UUID PRIMARY KEY,
            library_id UUID NOT NULL,
            command_id UUID NOT NULL,
            decision_payload_fingerprint VARCHAR(64) NOT NULL,
            operation_kind VARCHAR(32) NOT NULL,
            evaluated_outcome VARCHAR(32) NOT NULL,
            lifecycle_status VARCHAR(32) NOT NULL,
            successor_detail_snapshot JSONB NOT NULL,
            split_partition_snapshot JSONB NULL,
            projection_assignment_snapshot JSONB NOT NULL,
            reason_code VARCHAR(64) NOT NULL,
            reason_text VARCHAR(512) NOT NULL,
            method VARCHAR(64) NOT NULL,
            confidence NUMERIC(7,6) NULL,
            evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
            precondition_fingerprint VARCHAR(64) NOT NULL,
            observed_precondition_fingerprint VARCHAR(64) NOT NULL,
            supersedes_decision_id UUID NULL,
            actor_type VARCHAR(32) NOT NULL,
            actor_id UUID NOT NULL,
            request_id VARCHAR(128) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_canonical_entity_evolution_decisions_operation
                CHECK (operation_kind IN ('merge','split','reassign')),
            CONSTRAINT ck_canonical_entity_evolution_decisions_outcome
                CHECK (evaluated_outcome IN ('pending','applied','rejected','stale','cancelled')),
            CONSTRAINT ck_canonical_entity_evolution_decisions_lifecycle
                CHECK (lifecycle_status IN ('pending','applied','rejected','stale','cancelled','superseded')),
            CONSTRAINT ck_canonical_entity_evolution_decisions_confidence
                CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
            CONSTRAINT ck_canonical_entity_evolution_decisions_fingerprints
                CHECK (decision_payload_fingerprint ~ '^[0-9a-f]{64}$'
                   AND precondition_fingerprint ~ '^[0-9a-f]{64}$'
                   AND observed_precondition_fingerprint ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_canonical_entity_evolution_decisions_snapshots
                CHECK (jsonb_typeof(successor_detail_snapshot) = 'object'
                   AND jsonb_typeof(projection_assignment_snapshot) IN ('array','object')
                   AND jsonb_typeof(evidence_refs) = 'array'
                   AND (split_partition_snapshot IS NULL OR jsonb_typeof(split_partition_snapshot) = 'array')),
            CONSTRAINT ck_canonical_entity_evolution_decisions_actor_type
                CHECK (actor_type IN ('user','service')),
            CONSTRAINT ck_canonical_entity_evolution_decisions_not_self_predecessor
                CHECK (supersedes_decision_id IS NULL OR supersedes_decision_id <> id),
            CONSTRAINT uq_canonical_entity_evolution_decisions_id_library UNIQUE (id, library_id),
            CONSTRAINT uq_canonical_entity_evolution_decisions_id_library_command UNIQUE (id, library_id, command_id),
            CONSTRAINT uq_canonical_entity_evolution_decisions_payload UNIQUE (library_id, command_id, decision_payload_fingerprint),
            CONSTRAINT uq_canonical_entity_evolution_decisions_supersedes UNIQUE (library_id, supersedes_decision_id),
            CONSTRAINT fk_canonical_entity_evolution_decisions_command
                FOREIGN KEY (command_id, library_id)
                REFERENCES canonical_entity_evolution_commands(id, library_id) ON DELETE RESTRICT,
            CONSTRAINT fk_canonical_entity_evolution_decisions_supersedes
                FOREIGN KEY (supersedes_decision_id, library_id, command_id)
                REFERENCES canonical_entity_evolution_decisions(id, library_id, command_id) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE canonical_entity_evolution_sources (
            id UUID PRIMARY KEY,
            library_id UUID NOT NULL,
            command_id UUID NOT NULL,
            evolution_decision_id UUID NOT NULL,
            source_canonical_entity_id UUID NOT NULL,
            supersedes_source_transition_id UUID NULL,
            resolution_state VARCHAR(32) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_canonical_entity_evolution_sources_state
                CHECK (resolution_state IN ('pending','applied','superseded','historical_only')),
            CONSTRAINT ck_canonical_entity_evolution_sources_not_self_predecessor
                CHECK (supersedes_source_transition_id IS NULL OR supersedes_source_transition_id <> id),
            CONSTRAINT uq_canonical_entity_evolution_sources_id_library_command_source
                UNIQUE (id, library_id, command_id, source_canonical_entity_id),
            CONSTRAINT uq_canonical_entity_evolution_sources_id_library_command_decision
                UNIQUE (id, library_id, command_id, evolution_decision_id),
            CONSTRAINT uq_canonical_entity_evolution_sources_decision_source
                UNIQUE (library_id, evolution_decision_id, source_canonical_entity_id),
            CONSTRAINT fk_canonical_entity_evolution_sources_decision
                FOREIGN KEY (evolution_decision_id, library_id, command_id)
                REFERENCES canonical_entity_evolution_decisions(id, library_id, command_id) ON DELETE RESTRICT,
            CONSTRAINT fk_canonical_entity_evolution_sources_canonical
                FOREIGN KEY (source_canonical_entity_id, library_id)
                REFERENCES canonical_entities(id, library_id) ON DELETE RESTRICT,
            CONSTRAINT fk_canonical_entity_evolution_sources_supersedes
                FOREIGN KEY (supersedes_source_transition_id, library_id, command_id, source_canonical_entity_id)
                REFERENCES canonical_entity_evolution_sources(id, library_id, command_id, source_canonical_entity_id) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE canonical_entity_evolution_successors (
            id UUID PRIMARY KEY,
            library_id UUID NOT NULL,
            command_id UUID NOT NULL,
            evolution_decision_id UUID NOT NULL,
            source_transition_id UUID NOT NULL,
            target_ref_kind VARCHAR(32) NOT NULL,
            target_ref_key VARCHAR(256) NOT NULL,
            target_canonical_entity_id UUID NULL,
            target_spec_snapshot JSONB NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_canonical_entity_evolution_successors_target_kind
                CHECK (target_ref_kind IN ('existing','new')),
            CONSTRAINT ck_canonical_entity_evolution_successors_target_shape
                CHECK ((target_ref_kind = 'existing' AND target_canonical_entity_id IS NOT NULL AND target_spec_snapshot IS NULL)
                    OR (target_ref_kind = 'new' AND target_spec_snapshot IS NOT NULL)),
            CONSTRAINT ck_canonical_entity_evolution_successors_target_spec
                CHECK (target_spec_snapshot IS NULL OR jsonb_typeof(target_spec_snapshot) = 'object'),
            CONSTRAINT uq_canonical_entity_evolution_successors_id_library UNIQUE (id, library_id),
            CONSTRAINT uq_canonical_entity_evolution_successors_id_library_command_decision UNIQUE (id, library_id, command_id, evolution_decision_id),
            CONSTRAINT uq_canonical_entity_evolution_successors_target_slot
                UNIQUE (library_id, source_transition_id, target_ref_kind, target_ref_key),
            CONSTRAINT fk_canonical_entity_evolution_successors_decision
                FOREIGN KEY (evolution_decision_id, library_id, command_id)
                REFERENCES canonical_entity_evolution_decisions(id, library_id, command_id) ON DELETE RESTRICT,
            CONSTRAINT fk_canonical_entity_evolution_successors_source
                FOREIGN KEY (source_transition_id, library_id, command_id, evolution_decision_id)
                REFERENCES canonical_entity_evolution_sources(id, library_id, command_id, evolution_decision_id) ON DELETE RESTRICT,
            CONSTRAINT fk_canonical_entity_evolution_successors_target
                FOREIGN KEY (target_canonical_entity_id, library_id)
                REFERENCES canonical_entities(id, library_id) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE canonical_entity_projection_assignments (
            id UUID PRIMARY KEY,
            library_id UUID NOT NULL,
            command_id UUID NOT NULL,
            evolution_decision_id UUID NOT NULL,
            entity_id UUID NOT NULL,
            supersedes_assignment_id UUID NULL,
            from_canonical_entity_id UUID NOT NULL,
            target_successor_id UUID NULL,
            target_canonical_entity_id UUID NULL,
            assignment_state VARCHAR(32) NOT NULL,
            partition_basis_snapshot JSONB NOT NULL,
            reason_code VARCHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_canonical_entity_projection_assignments_state
                CHECK (assignment_state IN ('resolved','pending','superseded')),
            CONSTRAINT ck_canonical_entity_projection_assignments_not_self_target
                CHECK (target_canonical_entity_id IS NULL OR target_canonical_entity_id <> from_canonical_entity_id),
            CONSTRAINT ck_canonical_entity_projection_assignments_target_state
                CHECK (assignment_state = 'superseded'
                    OR (assignment_state = 'pending' AND target_successor_id IS NULL AND target_canonical_entity_id IS NULL)
                    OR (assignment_state = 'resolved' AND target_canonical_entity_id IS NOT NULL)),
            CONSTRAINT ck_canonical_entity_projection_assignments_basis
                CHECK (jsonb_typeof(partition_basis_snapshot) = 'object'),
            CONSTRAINT ck_canonical_entity_projection_assignments_not_self_predecessor
                CHECK (supersedes_assignment_id IS NULL OR supersedes_assignment_id <> id),
            CONSTRAINT uq_canonical_entity_projection_assignments_id_library_entity UNIQUE (id, library_id, entity_id),
            CONSTRAINT uq_canonical_entity_projection_assignments_id_library_command_entity UNIQUE (id, library_id, command_id, entity_id),
            CONSTRAINT uq_canonical_entity_projection_assignments_decision_entity UNIQUE (library_id, evolution_decision_id, entity_id),
            CONSTRAINT fk_canonical_entity_projection_assignments_decision
                FOREIGN KEY (evolution_decision_id, library_id, command_id)
                REFERENCES canonical_entity_evolution_decisions(id, library_id, command_id) ON DELETE RESTRICT,
            CONSTRAINT fk_canonical_entity_projection_assignments_entity
                FOREIGN KEY (entity_id, library_id) REFERENCES entities(id, library_id) ON DELETE RESTRICT,
            CONSTRAINT fk_canonical_entity_projection_assignments_from
                FOREIGN KEY (from_canonical_entity_id, library_id) REFERENCES canonical_entities(id, library_id) ON DELETE RESTRICT,
            CONSTRAINT fk_canonical_entity_projection_assignments_target
                FOREIGN KEY (target_canonical_entity_id, library_id) REFERENCES canonical_entities(id, library_id) ON DELETE RESTRICT,
            CONSTRAINT fk_canonical_entity_projection_assignments_supersedes
                FOREIGN KEY (supersedes_assignment_id, library_id, command_id, entity_id)
                REFERENCES canonical_entity_projection_assignments(id, library_id, command_id, entity_id) ON DELETE RESTRICT,
            CONSTRAINT fk_canonical_entity_projection_assignments_successor
                FOREIGN KEY (target_successor_id, library_id, command_id, evolution_decision_id)
                REFERENCES canonical_entity_evolution_successors(id, library_id, command_id, evolution_decision_id) ON DELETE RESTRICT
        )
        """
    )
    op.execute("ALTER TABLE entity_resolution_decisions ADD COLUMN evolution_assignment_id UUID NULL")
    op.execute(
        """
        ALTER TABLE entity_resolution_decisions
        ADD CONSTRAINT fk_entity_resolution_decisions_evolution_assignment
        FOREIGN KEY (evolution_assignment_id, library_id, entity_id)
        REFERENCES canonical_entity_projection_assignments(id, library_id, entity_id) ON DELETE RESTRICT
        """
    )
    op.execute(
        """
        ALTER TABLE entity_resolution_decisions
        ADD CONSTRAINT ck_entity_resolution_decisions_evolution_assignment
        CHECK (evolution_assignment_id IS NULL OR
            (decision_kind = 'link_existing' AND entity_id IS NOT NULL
             AND canonical_entity_id IS NOT NULL AND supersedes_decision_id IS NOT NULL))
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_canonical_entity_evolution_decisions_command_root "
        "ON canonical_entity_evolution_decisions (library_id, command_id) "
        "WHERE supersedes_decision_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_canonical_entity_evolution_decisions_pending "
        "ON canonical_entity_evolution_decisions (library_id, command_id) "
        "WHERE lifecycle_status = 'pending'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_canonical_entity_evolution_decisions_applied "
        "ON canonical_entity_evolution_decisions (library_id, command_id) "
        "WHERE lifecycle_status = 'applied'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_canonical_entity_evolution_sources_command_root "
        "ON canonical_entity_evolution_sources (library_id, command_id, source_canonical_entity_id) "
        "WHERE supersedes_source_transition_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_canonical_entity_evolution_sources_superseded_by "
        "ON canonical_entity_evolution_sources (library_id, supersedes_source_transition_id) "
        "WHERE supersedes_source_transition_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_canonical_entity_evolution_sources_current "
        "ON canonical_entity_evolution_sources (library_id, source_canonical_entity_id) "
        "WHERE resolution_state IN ('pending','applied')"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_canonical_entity_projection_assignments_command_root "
        "ON canonical_entity_projection_assignments (library_id, command_id, entity_id) "
        "WHERE supersedes_assignment_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_canonical_entity_projection_assignments_superseded_by "
        "ON canonical_entity_projection_assignments (library_id, supersedes_assignment_id) "
        "WHERE supersedes_assignment_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_entity_resolution_decisions_evolution_supersedes "
        "ON entity_resolution_decisions (library_id, supersedes_decision_id) "
        "WHERE evolution_assignment_id IS NOT NULL"
    )
    for table in _EVOLUTION_TABLES:
        index_name = {
            "canonical_entity_evolution_commands": "ix_canonical_entity_evolution_commands_library_id",
            "canonical_entity_evolution_decisions": "ix_canonical_entity_evolution_decisions_library_id",
            "canonical_entity_evolution_sources": "ix_canonical_entity_evolution_sources_library_id",
            "canonical_entity_evolution_successors": "ix_canonical_entity_evolution_successors_library_id",
            "canonical_entity_projection_assignments": "ix_canonical_entity_projection_assignments_library_id",
        }[table]
        op.execute(f"CREATE INDEX {index_name} ON {table} (library_id)")
    _create_target_triggers()


def _create_target_triggers() -> None:
    op.execute(
        """
        CREATE FUNCTION canonical_entity_evolution_append_only() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'canonical entity evolution audit is append-only';
            END IF;
            IF TG_TABLE_NAME IN ('canonical_entity_evolution_commands', 'canonical_entity_evolution_successors') THEN
                RAISE EXCEPTION 'canonical entity evolution row is immutable';
            END IF;
            IF TG_TABLE_NAME = 'canonical_entity_evolution_decisions' THEN
                IF NEW.lifecycle_status <> 'superseded'
                   OR OLD.lifecycle_status NOT IN ('pending','rejected','stale')
                   OR (to_jsonb(NEW) - 'lifecycle_status') <> (to_jsonb(OLD) - 'lifecycle_status') THEN
                    RAISE EXCEPTION 'canonical entity evolution decision update is invalid';
                END IF;
            ELSIF TG_TABLE_NAME = 'canonical_entity_evolution_sources' THEN
                IF NEW.resolution_state <> 'superseded'
                   OR OLD.resolution_state <> 'pending'
                   OR (to_jsonb(NEW) - 'resolution_state') <> (to_jsonb(OLD) - 'resolution_state') THEN
                    RAISE EXCEPTION 'canonical entity evolution source update is invalid';
                END IF;
            ELSIF TG_TABLE_NAME = 'canonical_entity_projection_assignments' THEN
                IF NEW.assignment_state <> 'superseded'
                   OR OLD.assignment_state NOT IN ('pending','resolved')
                   OR (to_jsonb(NEW) - 'assignment_state') <> (to_jsonb(OLD) - 'assignment_state') THEN
                    RAISE EXCEPTION 'canonical entity evolution assignment update is invalid';
                END IF;
            END IF;
            RETURN NEW;
        END $$ LANGUAGE plpgsql
        """
    )
    for table in _EVOLUTION_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER canonical_entity_evolution_append_only_{table}
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION canonical_entity_evolution_append_only()
            """
        )
    op.execute(
        """
        CREATE FUNCTION canonical_entity_evolution_chain() RETURNS trigger AS $$
        DECLARE command_scope UUID;
        DECLARE root_count INTEGER;
        DECLARE head_count INTEGER;
        BEGIN
            IF TG_TABLE_NAME = 'canonical_entity_evolution_commands' THEN
                command_scope := NEW.id;
            ELSE
                command_scope := NEW.command_id;
            END IF;
            SELECT count(*) INTO root_count
            FROM canonical_entity_evolution_decisions d
            WHERE d.command_id = command_scope AND d.library_id = NEW.library_id
              AND d.supersedes_decision_id IS NULL;
            SELECT count(*) INTO head_count
            FROM canonical_entity_evolution_decisions d
            WHERE d.command_id = command_scope AND d.library_id = NEW.library_id
              AND NOT EXISTS (
                  SELECT 1 FROM canonical_entity_evolution_decisions child
                  WHERE child.supersedes_decision_id = d.id AND child.library_id = d.library_id
              );
            IF root_count <> 1 OR head_count <> 1 THEN
                RAISE EXCEPTION 'canonical entity evolution chain is invalid';
            END IF;
            RETURN NULL;
        END $$ LANGUAGE plpgsql
        """
    )
    for table in (
        "canonical_entity_evolution_commands",
        "canonical_entity_evolution_decisions",
        "canonical_entity_evolution_sources",
        "canonical_entity_projection_assignments",
    ):
        op.execute(
            f"""
            CREATE CONSTRAINT TRIGGER canonical_entity_evolution_chain_{table}
            AFTER INSERT OR UPDATE ON {table}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION canonical_entity_evolution_chain()
            """
        )
    op.execute(
        """
        CREATE FUNCTION canonical_entity_evolution_integrity() RETURNS trigger AS $$
        DECLARE
            command_row RECORD;
            source_scope RECORD;
            assignment_scope RECORD;
            root_count INTEGER;
            head_count INTEGER;
            walked_count INTEGER;
            chain_cycle BOOLEAN;
        BEGIN
            -- Decisions, source transitions, and assignments are independent append-only
            -- chains.  Count/recursion checks deliberately run at COMMIT so a correction
            -- may stage its supersedes links and replacements in either flush order.
            FOR command_row IN
                SELECT id, library_id FROM canonical_entity_evolution_commands
            LOOP
                SELECT count(*) FILTER (WHERE supersedes_decision_id IS NULL),
                       count(*) FILTER (WHERE NOT EXISTS (
                           SELECT 1 FROM canonical_entity_evolution_decisions child
                           WHERE child.library_id = d.library_id
                             AND child.supersedes_decision_id = d.id
                       ))
                INTO root_count, head_count
                FROM canonical_entity_evolution_decisions d
                WHERE d.library_id = command_row.library_id AND d.command_id = command_row.id;
                IF root_count <> 1 OR head_count <> 1 THEN
                    RAISE EXCEPTION 'canonical entity evolution Decision chain is invalid';
                END IF;
                WITH RECURSIVE walk(id, predecessor_id, path, cycle) AS (
                    SELECT d.id, d.supersedes_decision_id, ARRAY[d.id], false
                    FROM canonical_entity_evolution_decisions d
                    WHERE d.library_id = command_row.library_id AND d.command_id = command_row.id
                      AND NOT EXISTS (
                          SELECT 1 FROM canonical_entity_evolution_decisions child
                          WHERE child.library_id = d.library_id
                            AND child.supersedes_decision_id = d.id
                      )
                    UNION ALL
                    SELECT parent.id, parent.supersedes_decision_id,
                           walk.path || parent.id, parent.id = ANY(walk.path)
                    FROM canonical_entity_evolution_decisions parent
                    JOIN walk ON parent.id = walk.predecessor_id
                    WHERE NOT walk.cycle
                )
                SELECT count(*), COALESCE(bool_or(cycle), false)
                INTO walked_count, chain_cycle FROM walk;
                IF chain_cycle OR walked_count <> (
                    SELECT count(*) FROM canonical_entity_evolution_decisions d
                    WHERE d.library_id = command_row.library_id AND d.command_id = command_row.id
                ) THEN
                    RAISE EXCEPTION 'canonical entity evolution Decision chain is cyclic or orphaned';
                END IF;
            END LOOP;

            FOR source_scope IN
                SELECT library_id, command_id, source_canonical_entity_id
                FROM canonical_entity_evolution_sources
                GROUP BY library_id, command_id, source_canonical_entity_id
            LOOP
                SELECT count(*) FILTER (WHERE supersedes_source_transition_id IS NULL),
                       count(*) FILTER (WHERE NOT EXISTS (
                           SELECT 1 FROM canonical_entity_evolution_sources child
                           WHERE child.library_id = s.library_id
                             AND child.supersedes_source_transition_id = s.id
                       ))
                INTO root_count, head_count
                FROM canonical_entity_evolution_sources s
                WHERE s.library_id = source_scope.library_id
                  AND s.command_id = source_scope.command_id
                  AND s.source_canonical_entity_id = source_scope.source_canonical_entity_id;
                IF root_count <> 1 OR head_count <> 1 THEN
                    RAISE EXCEPTION 'canonical entity evolution source chain is invalid';
                END IF;
                WITH RECURSIVE walk(id, predecessor_id, path, cycle) AS (
                    SELECT s.id, s.supersedes_source_transition_id, ARRAY[s.id], false
                    FROM canonical_entity_evolution_sources s
                    WHERE s.library_id = source_scope.library_id
                      AND s.command_id = source_scope.command_id
                      AND s.source_canonical_entity_id = source_scope.source_canonical_entity_id
                      AND NOT EXISTS (
                          SELECT 1 FROM canonical_entity_evolution_sources child
                          WHERE child.library_id = s.library_id
                            AND child.supersedes_source_transition_id = s.id
                      )
                    UNION ALL
                    SELECT parent.id, parent.supersedes_source_transition_id,
                           walk.path || parent.id, parent.id = ANY(walk.path)
                    FROM canonical_entity_evolution_sources parent
                    JOIN walk ON parent.id = walk.predecessor_id
                    WHERE NOT walk.cycle
                )
                SELECT count(*), COALESCE(bool_or(cycle), false)
                INTO walked_count, chain_cycle FROM walk;
                IF chain_cycle OR walked_count <> (
                    SELECT count(*) FROM canonical_entity_evolution_sources s
                    WHERE s.library_id = source_scope.library_id
                      AND s.command_id = source_scope.command_id
                      AND s.source_canonical_entity_id = source_scope.source_canonical_entity_id
                ) THEN
                    RAISE EXCEPTION 'canonical entity evolution source chain is cyclic or orphaned';
                END IF;
            END LOOP;

            FOR assignment_scope IN
                SELECT library_id, command_id, entity_id
                FROM canonical_entity_projection_assignments
                GROUP BY library_id, command_id, entity_id
            LOOP
                SELECT count(*) FILTER (WHERE supersedes_assignment_id IS NULL),
                       count(*) FILTER (WHERE NOT EXISTS (
                           SELECT 1 FROM canonical_entity_projection_assignments child
                           WHERE child.library_id = a.library_id
                             AND child.supersedes_assignment_id = a.id
                       ))
                INTO root_count, head_count
                FROM canonical_entity_projection_assignments a
                WHERE a.library_id = assignment_scope.library_id
                  AND a.command_id = assignment_scope.command_id
                  AND a.entity_id = assignment_scope.entity_id;
                IF root_count <> 1 OR head_count <> 1 THEN
                    RAISE EXCEPTION 'canonical entity evolution assignment chain is invalid';
                END IF;
                WITH RECURSIVE walk(id, predecessor_id, path, cycle) AS (
                    SELECT a.id, a.supersedes_assignment_id, ARRAY[a.id], false
                    FROM canonical_entity_projection_assignments a
                    WHERE a.library_id = assignment_scope.library_id
                      AND a.command_id = assignment_scope.command_id
                      AND a.entity_id = assignment_scope.entity_id
                      AND NOT EXISTS (
                          SELECT 1 FROM canonical_entity_projection_assignments child
                          WHERE child.library_id = a.library_id
                            AND child.supersedes_assignment_id = a.id
                      )
                    UNION ALL
                    SELECT parent.id, parent.supersedes_assignment_id,
                           walk.path || parent.id, parent.id = ANY(walk.path)
                    FROM canonical_entity_projection_assignments parent
                    JOIN walk ON parent.id = walk.predecessor_id
                    WHERE NOT walk.cycle
                )
                SELECT count(*), COALESCE(bool_or(cycle), false)
                INTO walked_count, chain_cycle FROM walk;
                IF chain_cycle OR walked_count <> (
                    SELECT count(*) FROM canonical_entity_projection_assignments a
                    WHERE a.library_id = assignment_scope.library_id
                      AND a.command_id = assignment_scope.command_id
                      AND a.entity_id = assignment_scope.entity_id
                ) THEN
                    RAISE EXCEPTION 'canonical entity evolution assignment chain is cyclic or orphaned';
                END IF;
            END LOOP;

            IF EXISTS (
                SELECT 1
                FROM canonical_entity_evolution_successors successor
                JOIN canonical_entity_evolution_sources source ON source.id = successor.source_transition_id
                JOIN canonical_entity_evolution_decisions decision ON decision.id = successor.evolution_decision_id
                WHERE (decision.lifecycle_status = 'applied' AND (
                           successor.target_canonical_entity_id IS NULL
                        OR successor.target_canonical_entity_id = source.source_canonical_entity_id
                    ))
                   OR (successor.target_ref_kind = 'existing' AND successor.target_ref_key <> successor.target_canonical_entity_id::text)
                   OR (successor.target_ref_kind = 'new' AND decision.lifecycle_status = 'pending'
                       AND successor.target_canonical_entity_id IS NOT NULL)
                   OR (successor.target_ref_kind = 'new' AND decision.lifecycle_status = 'applied'
                       AND successor.target_canonical_entity_id IS NULL)
            ) THEN
                RAISE EXCEPTION 'canonical entity evolution successor state is invalid';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM canonical_entity_evolution_sources source
                JOIN canonical_entity_evolution_decisions decision ON decision.id = source.evolution_decision_id
                WHERE source.resolution_state = 'historical_only'
                  AND decision.operation_kind IN ('merge', 'split', 'reassign')
            ) THEN
                RAISE EXCEPTION 'P3.1 operations cannot create historical_only source states';
            END IF;

            IF EXISTS (
                SELECT 1 FROM canonical_entity_evolution_decisions decision
                WHERE decision.lifecycle_status IN ('rejected', 'stale')
                  AND (EXISTS (SELECT 1 FROM canonical_entity_evolution_sources s WHERE s.evolution_decision_id = decision.id)
                    OR EXISTS (SELECT 1 FROM canonical_entity_evolution_successors s WHERE s.evolution_decision_id = decision.id)
                    OR EXISTS (SELECT 1 FROM canonical_entity_projection_assignments a WHERE a.evolution_decision_id = decision.id))
            ) THEN
                RAISE EXCEPTION 'audit-only Decision has children';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM canonical_entity_evolution_decisions decision
                WHERE decision.lifecycle_status = 'cancelled'
                  AND (decision.evaluated_outcome <> 'cancelled'
                    OR decision.successor_detail_snapshot <> '{}'::jsonb
                    OR decision.projection_assignment_snapshot <> '{}'::jsonb
                    OR decision.split_partition_snapshot IS NOT NULL
                    OR EXISTS (SELECT 1 FROM canonical_entity_evolution_sources s WHERE s.evolution_decision_id = decision.id)
                    OR EXISTS (SELECT 1 FROM canonical_entity_evolution_successors s WHERE s.evolution_decision_id = decision.id)
                    OR EXISTS (SELECT 1 FROM canonical_entity_projection_assignments a WHERE a.evolution_decision_id = decision.id))
            ) THEN
                RAISE EXCEPTION 'cancelled Decision must be a no-child terminal control row';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM canonical_entity_projection_assignments assignment
                JOIN canonical_entity_evolution_decisions decision ON decision.id = assignment.evolution_decision_id
                LEFT JOIN canonical_entity_evolution_successors successor ON successor.id = assignment.target_successor_id
                LEFT JOIN canonical_entity_evolution_sources source ON source.id = successor.source_transition_id
                WHERE (assignment.assignment_state = 'pending' AND (
                           assignment.target_canonical_entity_id IS NOT NULL
                        OR assignment.target_successor_id IS NOT NULL
                    ))
                   OR (assignment.assignment_state = 'resolved' AND decision.operation_kind IN ('merge', 'split')
                       AND (successor.id IS NULL OR successor.target_canonical_entity_id <> assignment.target_canonical_entity_id
                            OR successor.command_id <> assignment.command_id
                            OR successor.evolution_decision_id <> assignment.evolution_decision_id
                            OR source.source_canonical_entity_id <> assignment.from_canonical_entity_id))
                   OR (assignment.assignment_state = 'resolved' AND decision.operation_kind = 'reassign'
                       AND assignment.target_successor_id IS NOT NULL)
            ) THEN
                RAISE EXCEPTION 'canonical entity evolution assignment slot is invalid';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM canonical_entity_projection_assignments assignment
                JOIN canonical_entity_evolution_decisions decision ON decision.id = assignment.evolution_decision_id
                JOIN entities entity ON entity.id = assignment.entity_id AND entity.library_id = assignment.library_id
                WHERE decision.lifecycle_status = 'applied'
                  AND assignment.assignment_state = 'resolved'
                  AND entity.canonical_entity_id IS DISTINCT FROM assignment.target_canonical_entity_id
            ) THEN
                RAISE EXCEPTION 'Entity pointer is not paired with its applied evolution assignment';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM entity_resolution_decisions replacement
                JOIN canonical_entity_projection_assignments assignment ON assignment.id = replacement.evolution_assignment_id
                JOIN canonical_entity_evolution_decisions decision ON decision.id = assignment.evolution_decision_id
                LEFT JOIN entity_resolution_decisions previous ON previous.id = replacement.supersedes_decision_id
                WHERE replacement.evolution_assignment_id IS NOT NULL
                  AND (decision.lifecycle_status <> 'applied'
                    OR replacement.lifecycle_status <> 'active'
                    OR replacement.decision_kind <> 'link_existing'
                    OR replacement.canonical_entity_id <> assignment.target_canonical_entity_id
                    OR previous.id IS NULL
                    OR previous.lifecycle_status <> 'superseded'
                    OR previous.entity_id <> replacement.entity_id
                    OR previous.subject_fingerprint <> replacement.subject_fingerprint
                    OR previous.canonical_entity_id <> assignment.from_canonical_entity_id)
            ) THEN
                RAISE EXCEPTION 'EntityResolutionDecision evolution replacement is invalid';
            END IF;
            RETURN NULL;
        END $$ LANGUAGE plpgsql
        """
    )
    for table in (*_EVOLUTION_TABLES, "entities", "entity_resolution_decisions"):
        op.execute(
            f"""
            CREATE CONSTRAINT TRIGGER canonical_entity_evolution_integrity_{table}
            AFTER INSERT OR UPDATE ON {table}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION canonical_entity_evolution_integrity()
            """
        )


def _drop_target_schema() -> None:
    for table in (*_EVOLUTION_TABLES, "entities", "entity_resolution_decisions"):
        op.execute(f"DROP TRIGGER IF EXISTS canonical_entity_evolution_integrity_{table} ON {table}")
    for table in (
        "canonical_entity_evolution_commands",
        "canonical_entity_evolution_decisions",
        "canonical_entity_evolution_sources",
        "canonical_entity_evolution_successors",
        "canonical_entity_projection_assignments",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS canonical_entity_evolution_chain_{table} ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS canonical_entity_evolution_append_only_{table} ON {table}")
    op.execute("DROP FUNCTION canonical_entity_evolution_chain()")
    op.execute("DROP FUNCTION canonical_entity_evolution_integrity()")
    op.execute("DROP FUNCTION canonical_entity_evolution_append_only()")
    op.execute(
        "ALTER TABLE entity_resolution_decisions "
        "DROP CONSTRAINT fk_entity_resolution_decisions_evolution_assignment"
    )
    op.execute(
        "ALTER TABLE entity_resolution_decisions "
        "DROP CONSTRAINT ck_entity_resolution_decisions_evolution_assignment"
    )
    op.execute("DROP INDEX uq_entity_resolution_decisions_evolution_supersedes")
    op.execute("ALTER TABLE entity_resolution_decisions DROP COLUMN evolution_assignment_id")
    for table in reversed(_EVOLUTION_TABLES):
        op.execute(f"DROP TABLE {table}")


def _create_0071_schema() -> None:
    """Restore the pre-correction shape only after the target audit tables are empty."""

    op.execute(
        """
        CREATE TABLE canonical_entity_evolution_commands (
            id UUID PRIMARY KEY, library_id UUID NOT NULL REFERENCES sys_libraries(id) ON DELETE RESTRICT,
            idempotency_key VARCHAR(256) NOT NULL, request_fingerprint VARCHAR(64) NOT NULL,
            operation_kind VARCHAR(32) NOT NULL, contract_version VARCHAR(64) NOT NULL,
            supersedes_command_id UUID NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_canonical_entity_evolution_commands_operation CHECK (operation_kind IN ('merge','split','reassign')),
            CONSTRAINT ck_canonical_entity_evolution_commands_fingerprint CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
            CONSTRAINT uq_canonical_entity_evolution_commands_id_library UNIQUE (id, library_id),
            CONSTRAINT uq_canonical_entity_evolution_commands_idempotency UNIQUE (library_id, idempotency_key),
            CONSTRAINT uq_canonical_entity_evolution_commands_supersedes UNIQUE (library_id, supersedes_command_id),
            CONSTRAINT fk_canonical_entity_evolution_commands_supersedes FOREIGN KEY (supersedes_command_id, library_id)
                REFERENCES canonical_entity_evolution_commands(id, library_id) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE canonical_entity_evolution_decisions (
            id UUID PRIMARY KEY, library_id UUID NOT NULL, command_id UUID NOT NULL,
            operation_kind VARCHAR(32) NOT NULL, lifecycle_status VARCHAR(32) NOT NULL,
            reason_code VARCHAR(64) NOT NULL, reason_text VARCHAR(512) NOT NULL, method VARCHAR(64) NOT NULL,
            confidence DOUBLE PRECISION NULL, evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
            precondition_fingerprint VARCHAR(64) NOT NULL, supersedes_decision_id UUID NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_canonical_entity_evolution_decisions_operation CHECK (operation_kind IN ('merge','split','reassign')),
            CONSTRAINT ck_canonical_entity_evolution_decisions_lifecycle CHECK (lifecycle_status IN ('pending','applied','rejected','stale','superseded')),
            CONSTRAINT ck_canonical_entity_evolution_decisions_confidence CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
            CONSTRAINT ck_canonical_entity_evolution_decisions_precondition CHECK (precondition_fingerprint ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_canonical_entity_evolution_decisions_evidence CHECK (jsonb_typeof(evidence_refs) = 'array'),
            CONSTRAINT uq_canonical_entity_evolution_decisions_id_library UNIQUE (id, library_id),
            CONSTRAINT uq_canonical_entity_evolution_decisions_id_library_command UNIQUE (id, library_id, command_id),
            CONSTRAINT uq_canonical_entity_evolution_decisions_supersedes UNIQUE (library_id, supersedes_decision_id),
            CONSTRAINT fk_canonical_entity_evolution_decisions_command FOREIGN KEY (command_id, library_id)
                REFERENCES canonical_entity_evolution_commands(id, library_id) ON DELETE RESTRICT,
            CONSTRAINT fk_canonical_entity_evolution_decisions_supersedes FOREIGN KEY (supersedes_decision_id, library_id, command_id)
                REFERENCES canonical_entity_evolution_decisions(id, library_id, command_id) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE canonical_entity_evolution_sources (
            id UUID PRIMARY KEY, library_id UUID NOT NULL, evolution_decision_id UUID NOT NULL,
            source_canonical_entity_id UUID NOT NULL, resolution_state VARCHAR(32) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_canonical_entity_evolution_sources_state CHECK (resolution_state IN ('pending','applied','superseded','historical_only')),
            CONSTRAINT uq_canonical_entity_evolution_sources_id_library UNIQUE (id, library_id),
            CONSTRAINT uq_canonical_entity_evolution_sources_decision_source UNIQUE (library_id, evolution_decision_id, source_canonical_entity_id),
            CONSTRAINT fk_canonical_entity_evolution_sources_decision FOREIGN KEY (evolution_decision_id, library_id)
                REFERENCES canonical_entity_evolution_decisions(id, library_id) ON DELETE RESTRICT,
            CONSTRAINT fk_canonical_entity_evolution_sources_canonical FOREIGN KEY (source_canonical_entity_id, library_id)
                REFERENCES canonical_entities(id, library_id) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE canonical_entity_evolution_successors (
            id UUID PRIMARY KEY, library_id UUID NOT NULL, source_transition_id UUID NOT NULL,
            target_canonical_entity_id UUID NULL, target_spec_snapshot JSONB NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_canonical_entity_evolution_successors_target_spec CHECK (target_spec_snapshot IS NULL OR jsonb_typeof(target_spec_snapshot) = 'object'),
            CONSTRAINT uq_canonical_entity_evolution_successors_target UNIQUE (library_id, source_transition_id, target_canonical_entity_id),
            CONSTRAINT fk_canonical_entity_evolution_successors_source FOREIGN KEY (source_transition_id, library_id)
                REFERENCES canonical_entity_evolution_sources(id, library_id) ON DELETE RESTRICT,
            CONSTRAINT fk_canonical_entity_evolution_successors_target FOREIGN KEY (target_canonical_entity_id, library_id)
                REFERENCES canonical_entities(id, library_id) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE canonical_entity_projection_assignments (
            id UUID PRIMARY KEY, library_id UUID NOT NULL, evolution_decision_id UUID NOT NULL,
            entity_id UUID NOT NULL, from_canonical_entity_id UUID NOT NULL, target_canonical_entity_id UUID NULL,
            assignment_state VARCHAR(32) NOT NULL, partition_basis_snapshot JSONB NOT NULL,
            reason_code VARCHAR(64) NOT NULL, previous_entity_resolution_decision_id UUID NULL,
            new_entity_resolution_decision_id UUID NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_canonical_entity_projection_assignments_state CHECK (assignment_state IN ('resolved','pending','rejected')),
            CONSTRAINT ck_canonical_entity_projection_assignments_basis CHECK (jsonb_typeof(partition_basis_snapshot) = 'object'),
            CONSTRAINT uq_canonical_entity_projection_assignments_decision_entity UNIQUE (library_id, evolution_decision_id, entity_id),
            CONSTRAINT fk_canonical_entity_projection_assignments_decision FOREIGN KEY (evolution_decision_id, library_id)
                REFERENCES canonical_entity_evolution_decisions(id, library_id) ON DELETE RESTRICT,
            CONSTRAINT fk_canonical_entity_projection_assignments_entity FOREIGN KEY (entity_id, library_id)
                REFERENCES entities(id, library_id) ON DELETE RESTRICT,
            CONSTRAINT fk_canonical_entity_projection_assignments_from FOREIGN KEY (from_canonical_entity_id, library_id)
                REFERENCES canonical_entities(id, library_id) ON DELETE RESTRICT,
            CONSTRAINT fk_canonical_entity_projection_assignments_target FOREIGN KEY (target_canonical_entity_id, library_id)
                REFERENCES canonical_entities(id, library_id) ON DELETE RESTRICT,
            CONSTRAINT fk_canonical_entity_projection_assignments_previous_decision FOREIGN KEY (previous_entity_resolution_decision_id, library_id)
                REFERENCES entity_resolution_decisions(id, library_id) ON DELETE RESTRICT,
            CONSTRAINT fk_canonical_entity_projection_assignments_new_decision FOREIGN KEY (new_entity_resolution_decision_id, library_id)
                REFERENCES entity_resolution_decisions(id, library_id) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_canonical_entity_evolution_commands_library_id "
        "ON canonical_entity_evolution_commands (library_id)"
    )
    op.execute(
        "CREATE INDEX ix_canonical_entity_evolution_decisions_library_id "
        "ON canonical_entity_evolution_decisions (library_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_canonical_entity_evolution_decisions_pending "
        "ON canonical_entity_evolution_decisions (library_id, command_id) "
        "WHERE lifecycle_status = 'pending'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_canonical_entity_evolution_decisions_applied "
        "ON canonical_entity_evolution_decisions (library_id, command_id) "
        "WHERE lifecycle_status = 'applied'"
    )
    op.execute(
        "CREATE INDEX ix_canonical_entity_evolution_sources_library_id "
        "ON canonical_entity_evolution_sources (library_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_canonical_entity_evolution_sources_current "
        "ON canonical_entity_evolution_sources (library_id, source_canonical_entity_id) "
        "WHERE resolution_state IN ('pending','applied')"
    )
    op.execute(
        "CREATE INDEX ix_canonical_entity_evolution_successors_library_id "
        "ON canonical_entity_evolution_successors (library_id)"
    )
    op.execute(
        "CREATE INDEX ix_canonical_entity_projection_assignments_library_id "
        "ON canonical_entity_projection_assignments (library_id)"
    )


def upgrade() -> None:
    _maintenance_lock_and_empty_gate("P3_1_0072_NONEMPTY_LEGACY_AUDIT")
    _drop_0071_schema()
    _create_target_schema()


def downgrade() -> None:
    _maintenance_lock_and_empty_gate("P3_1_0072_NONEMPTY_TARGET_AUDIT")
    _drop_target_schema()
    _create_0071_schema()
