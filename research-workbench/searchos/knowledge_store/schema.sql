-- Research knowledge schema v1. Seven dimensions follow the user's reference image.
-- Identity/catalog rows describe objects. Evidence-bearing facts live in claims
-- and exactly one corresponding detail table; accepted_* views are read projections.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS database_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dimensions (
    id INTEGER PRIMARY KEY CHECK(id BETWEEN 1 AND 7), name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dimension_elements (
    id TEXT PRIMARY KEY, dimension_id INTEGER NOT NULL REFERENCES dimensions(id),
    name TEXT NOT NULL, ordinal INTEGER NOT NULL, primary_table TEXT NOT NULL,
    storage_rule TEXT NOT NULL, UNIQUE(dimension_id, ordinal), UNIQUE(dimension_id, name)
);
CREATE TABLE IF NOT EXISTS entity_kinds (id TEXT PRIMARY KEY, label TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS topics (
    id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL REFERENCES entity_kinds(id),
    canonical_name TEXT NOT NULL CHECK(length(trim(canonical_name)) > 0),
    namespace TEXT NOT NULL, identity_key TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','merged','retired')),
    merged_into_id TEXT REFERENCES entities(id), created_at TEXT NOT NULL,
    CHECK(merged_into_id IS NULL OR merged_into_id <> id),
    UNIQUE(namespace, kind, identity_key)
);
CREATE TABLE IF NOT EXISTS topic_entities (
    topic_id TEXT NOT NULL REFERENCES topics(id), entity_id TEXT NOT NULL REFERENCES entities(id),
    membership_role TEXT NOT NULL DEFAULT 'subject'
        CHECK(membership_role IN ('subject','shared_concept','external_reference','participant')),
    PRIMARY KEY(topic_id, entity_id)
);
CREATE TABLE IF NOT EXISTS entity_names (
    id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id), name TEXT NOT NULL,
    language TEXT, name_type TEXT NOT NULL
        CHECK(name_type IN ('canonical','alias','abbreviation','former','translation','code')),
    valid_from TEXT, valid_to TEXT, UNIQUE(entity_id, name, name_type)
);
CREATE TABLE IF NOT EXISTS entity_identifiers (
    id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
    scheme TEXT NOT NULL, value TEXT NOT NULL, UNIQUE(entity_id, scheme, value)
);
CREATE TABLE IF NOT EXISTS terms (
    id TEXT PRIMARY KEY, scheme TEXT NOT NULL, label TEXT NOT NULL,
    parent_id TEXT REFERENCES terms(id), description TEXT NOT NULL DEFAULT '',
    UNIQUE(scheme,label), CHECK(parent_id IS NULL OR parent_id <> id)
);

-- Dimension 1: technical identity; 3: routes; 4: composition.
CREATE TABLE IF NOT EXISTS technologies (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id),
    technology_type TEXT, abstraction_level TEXT
);
CREATE TABLE IF NOT EXISTS technology_routes (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id), route_type TEXT
);
CREATE TABLE IF NOT EXISTS technology_generations (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id),
    generation_label TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS components (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id), component_type TEXT
);
CREATE TABLE IF NOT EXISTS materials (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id), material_type TEXT
);
CREATE TABLE IF NOT EXISTS processes (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id), process_type TEXT
);
CREATE TABLE IF NOT EXISTS interfaces (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id), interface_type TEXT
);

-- Dimension 6: actors and shared research outputs.
CREATE TABLE IF NOT EXISTS persons (entity_id TEXT PRIMARY KEY REFERENCES entities(id));
CREATE TABLE IF NOT EXISTS organizations (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id), organization_type TEXT
);
CREATE TABLE IF NOT EXISTS research_teams (entity_id TEXT PRIMARY KEY REFERENCES entities(id));
CREATE TABLE IF NOT EXISTS achievements (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id), achievement_type TEXT
);
CREATE TABLE IF NOT EXISTS reference_implementations (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id), repository_url TEXT,
    execution_status TEXT NOT NULL DEFAULT 'not_run'
        CHECK(execution_status IN ('not_run','passed','failed','partial'))
);

-- Dimension 7: a family, model, version or configuration is a separate product.
CREATE TABLE IF NOT EXISTS products (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id),
    granularity TEXT NOT NULL DEFAULT 'undetermined'
        CHECK(granularity IN ('family','model','version','configuration','module','undetermined'))
);
CREATE TABLE IF NOT EXISTS application_scenarios (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id), scenario_type TEXT
);
CREATE TABLE IF NOT EXISTS application_cases (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id), application_type TEXT
);
CREATE TABLE IF NOT EXISTS technology_events (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id), event_type TEXT NOT NULL
);

-- Dimension 5: metric definition != an observation or an assessment.
CREATE TABLE IF NOT EXISTS units (
    id TEXT PRIMARY KEY, symbol TEXT NOT NULL UNIQUE, quantity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metric_definitions (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id), quantity TEXT,
    preferred_unit_id TEXT REFERENCES units(id), comparison_rule TEXT NOT NULL DEFAULT 'context_required'
);
CREATE TABLE IF NOT EXISTS metric_sets (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS metric_set_members (
    set_id TEXT NOT NULL REFERENCES metric_sets(id),
    metric_id TEXT NOT NULL REFERENCES metric_definitions(entity_id),
    PRIMARY KEY(set_id, metric_id)
);
CREATE TABLE IF NOT EXISTS capabilities (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id),
    parent_capability_id TEXT REFERENCES capabilities(entity_id)
);
CREATE TABLE IF NOT EXISTS test_contexts (
    id TEXT PRIMARY KEY, label TEXT NOT NULL,
    conditions_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(conditions_json)),
    completeness TEXT NOT NULL DEFAULT 'partial' CHECK(completeness IN ('partial','specified'))
);
CREATE TABLE IF NOT EXISTS assessment_scales (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, levels_json TEXT NOT NULL CHECK(json_valid(levels_json))
);

-- Versioned sources are independent of topics and reusable as evidence.
CREATE TABLE IF NOT EXISTS source_documents (
    id TEXT PRIMARY KEY, canonical_uri TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
    source_type TEXT NOT NULL, publisher_entity_id TEXT REFERENCES organizations(entity_id)
);
CREATE TABLE IF NOT EXISTS source_revisions (
    id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES source_documents(id),
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64), artifact_path TEXT NOT NULL,
    published_at TEXT, captured_at TEXT NOT NULL, UNIQUE(document_id,content_sha256)
);
CREATE TABLE IF NOT EXISTS source_segments (
    id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES source_revisions(id),
    locator TEXT NOT NULL, text_content TEXT NOT NULL, UNIQUE(revision_id,locator)
);
CREATE TABLE IF NOT EXISTS extraction_runs (
    id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES topics(id),
    revision_id TEXT NOT NULL REFERENCES source_revisions(id),
    schema_version TEXT NOT NULL, extractor_version TEXT NOT NULL, scope_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','partial','failed')),
    started_at TEXT, finished_at TEXT, error TEXT,
    UNIQUE(topic_id,revision_id,schema_version,extractor_version,scope_key)
);

CREATE TABLE IF NOT EXISTS field_groups (
    id TEXT PRIMARY KEY, subject_kind TEXT NOT NULL REFERENCES entity_kinds(id),
    group_key TEXT NOT NULL, label TEXT NOT NULL, UNIQUE(subject_kind,group_key)
);
CREATE TABLE IF NOT EXISTS field_definitions (
    id TEXT PRIMARY KEY, subject_kind TEXT NOT NULL REFERENCES entity_kinds(id),
    label TEXT NOT NULL, legacy_label TEXT NOT NULL, value_type TEXT NOT NULL,
    repeat_group TEXT, is_function INTEGER NOT NULL CHECK(is_function IN (0,1)),
    is_derived INTEGER NOT NULL CHECK(is_derived IN (0,1)), source_schema_version TEXT NOT NULL,
    definition_json TEXT NOT NULL CHECK(json_valid(definition_json))
);
CREATE TABLE IF NOT EXISTS field_group_members (
    field_id TEXT NOT NULL REFERENCES field_definitions(id),
    group_id TEXT NOT NULL REFERENCES field_groups(id), PRIMARY KEY(field_id,group_id)
);
CREATE TABLE IF NOT EXISTS field_storage_mappings (
    field_id TEXT PRIMARY KEY REFERENCES field_definitions(id),
    element_id TEXT NOT NULL REFERENCES dimension_elements(id),
    target_table TEXT NOT NULL, storage_rule TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('attribute','identity','metric','capability','relation',
        'event','participation','assessment','repeat_row','provenance','derived'))
);
CREATE TABLE IF NOT EXISTS relation_types (
    id TEXT PRIMARY KEY, label TEXT NOT NULL,
    source_kinds_json TEXT NOT NULL CHECK(json_valid(source_kinds_json)),
    target_kinds_json TEXT NOT NULL CHECK(json_valid(target_kinds_json)),
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS participation_roles (
    id TEXT PRIMARY KEY, label TEXT NOT NULL,
    subject_kinds_json TEXT NOT NULL CHECK(json_valid(subject_kinds_json)),
    object_kinds_json TEXT NOT NULL CHECK(json_valid(object_kinds_json))
);

-- Every fact has a topic, subject, time/scope, origin and review status.
-- Multiple observations are retained; acceptance never chooses a maximum/latest value.
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES topics(id),
    subject_id TEXT NOT NULL REFERENCES entities(id),
    claim_kind TEXT NOT NULL CHECK(claim_kind IN ('field','relation','metric','capability',
        'event','participation','assessment','maturity','composition','interface','route_step',
        'classification','identity')),
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK(status IN ('candidate','accepted','disputed','withdrawn')),
    assertion_type TEXT NOT NULL DEFAULT 'source_statement'
        CHECK(assertion_type IN ('source_statement','reported_statement','measurement',
            'analyst_assessment','model_suggestion','derived')),
    context_id TEXT REFERENCES test_contexts(id),
    scope_product_id TEXT REFERENCES products(entity_id),
    scope_note TEXT NOT NULL DEFAULT '', valid_from TEXT, valid_to TEXT,
    extraction_run_id TEXT REFERENCES extraction_runs(id),
    supersedes_id TEXT REFERENCES claims(id),
    review_locked INTEGER NOT NULL DEFAULT 0 CHECK(review_locked IN (0,1)),
    recorded_at TEXT NOT NULL, CHECK(supersedes_id IS NULL OR supersedes_id <> id)
);
CREATE TABLE IF NOT EXISTS claim_evidence (
    id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES claims(id),
    segment_id TEXT NOT NULL REFERENCES source_segments(id), quote TEXT NOT NULL,
    stance TEXT NOT NULL DEFAULT 'supports' CHECK(stance IN ('supports','refutes','context')),
    UNIQUE(claim_id,segment_id,quote,stance)
);
CREATE TABLE IF NOT EXISTS field_values (
    claim_id TEXT PRIMARY KEY REFERENCES claims(id), field_id TEXT NOT NULL REFERENCES field_definitions(id),
    row_key TEXT, value_json TEXT NOT NULL CHECK(json_valid(value_json) AND json_type(value_json)<>'null'),
    qualifiers_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(qualifiers_json))
);
CREATE TABLE IF NOT EXISTS entity_relations (
    claim_id TEXT PRIMARY KEY REFERENCES claims(id), relation_type_id TEXT NOT NULL REFERENCES relation_types(id),
    object_id TEXT NOT NULL REFERENCES entities(id),
    function_field_id TEXT REFERENCES field_definitions(id)
);
CREATE TABLE IF NOT EXISTS classifications (
    claim_id TEXT PRIMARY KEY REFERENCES claims(id), term_id TEXT NOT NULL REFERENCES terms(id)
);
CREATE TABLE IF NOT EXISTS identity_assertions (
    claim_id TEXT PRIMARY KEY REFERENCES claims(id), name_id TEXT REFERENCES entity_names(id),
    identifier_id TEXT REFERENCES entity_identifiers(id),
    CHECK((name_id IS NOT NULL)+(identifier_id IS NOT NULL)=1)
);
CREATE TABLE IF NOT EXISTS metric_observations (
    claim_id TEXT PRIMARY KEY REFERENCES claims(id),
    metric_id TEXT NOT NULL REFERENCES metric_definitions(entity_id),
    raw_value TEXT NOT NULL, numeric_value REAL, min_value REAL, max_value REAL,
    unit_id TEXT REFERENCES units(id), raw_unit TEXT,
    comparator TEXT CHECK(comparator IN ('=','>','>=','<','<=','approx')),
    statistic TEXT NOT NULL DEFAULT 'unspecified',
    value_nature TEXT NOT NULL CHECK(value_nature IN ('design_specification','vendor_claim',
        'third_party_measurement','independent_retest','estimate','model_derived','unspecified')),
    measured_at TEXT, method TEXT,
    CHECK(min_value IS NULL OR max_value IS NULL OR min_value<=max_value),
    CHECK(numeric_value IS NULL OR (min_value IS NULL AND max_value IS NULL))
);
CREATE TABLE IF NOT EXISTS product_capabilities (
    claim_id TEXT PRIMARY KEY REFERENCES claims(id), capability_id TEXT NOT NULL REFERENCES capabilities(entity_id),
    support_status TEXT NOT NULL CHECK(support_status IN ('supported','partial','planned',
        'claimed_unverified','unsupported')),
    function_field_id TEXT REFERENCES field_definitions(id),
    change_type TEXT CHECK(change_type IN ('added','enhanced','removed','deprecated','unspecified'))
);
CREATE TABLE IF NOT EXISTS event_details (
    claim_id TEXT PRIMARY KEY REFERENCES claims(id), occurred_at TEXT,
    date_precision TEXT NOT NULL DEFAULT 'unknown'
        CHECK(date_precision IN ('year','quarter','month','day','range','unknown')),
    date_qualifier TEXT NOT NULL DEFAULT 'unspecified'
        CHECK(date_qualifier IN ('exact','approx','not_before','not_after','unspecified')),
    ended_at TEXT, description TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS participations (
    claim_id TEXT PRIMARY KEY REFERENCES claims(id), role_id TEXT NOT NULL REFERENCES participation_roles(id),
    object_id TEXT NOT NULL REFERENCES entities(id)
);
CREATE TABLE IF NOT EXISTS assessments (
    claim_id TEXT PRIMARY KEY REFERENCES claims(id),
    assessment_type TEXT NOT NULL CHECK(assessment_type IN ('technical_level','advantage','limitation',
        'performance_change','route_feature','route_status','innovation')),
    comparator_entity_id TEXT REFERENCES entities(id), assessor_id TEXT REFERENCES entities(id),
    method TEXT, conclusion TEXT NOT NULL, assessed_at TEXT
);
CREATE TABLE IF NOT EXISTS assessment_basis (
    assessment_claim_id TEXT NOT NULL REFERENCES assessments(claim_id),
    basis_claim_id TEXT NOT NULL REFERENCES claims(id),
    PRIMARY KEY(assessment_claim_id,basis_claim_id), CHECK(assessment_claim_id<>basis_claim_id)
);
CREATE TABLE IF NOT EXISTS maturity_assessments (
    claim_id TEXT PRIMARY KEY REFERENCES claims(id), scale_id TEXT NOT NULL REFERENCES assessment_scales(id),
    level TEXT NOT NULL, assessor_id TEXT REFERENCES entities(id), assessed_at TEXT, rationale TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS composition_items (
    claim_id TEXT PRIMARY KEY REFERENCES claims(id), component_id TEXT NOT NULL REFERENCES entities(id),
    row_key TEXT NOT NULL, role TEXT NOT NULL, quantity REAL CHECK(quantity IS NULL OR quantity>=0),
    configuration TEXT, replaceable INTEGER CHECK(replaceable IN (0,1)), version_constraint TEXT
);
CREATE TABLE IF NOT EXISTS interface_connections (
    claim_id TEXT PRIMARY KEY REFERENCES claims(id), interface_id TEXT NOT NULL REFERENCES interfaces(entity_id),
    direction TEXT, protocol_id TEXT REFERENCES technologies(entity_id), protocol_version TEXT,
    compatible_entity_id TEXT REFERENCES entities(id), conformity_status TEXT
);
CREATE TABLE IF NOT EXISTS route_steps (
    claim_id TEXT PRIMARY KEY REFERENCES claims(id), ordinal INTEGER NOT NULL CHECK(ordinal>0),
    step_entity_id TEXT REFERENCES entities(id), description TEXT NOT NULL, row_key TEXT NOT NULL
);

-- Missingness is workflow state, never a value/false in a fact cell.
CREATE TABLE IF NOT EXISTS maintenance_tasks (
    id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES topics(id), entity_id TEXT REFERENCES entities(id),
    element_id TEXT REFERENCES dimension_elements(id), field_id TEXT REFERENCES field_definitions(id),
    task_type TEXT NOT NULL CHECK(task_type IN ('collect','extract','review','resolve_identity','refresh','migrate')),
    deduplication_key TEXT NOT NULL UNIQUE, priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','completed','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts>0),
    next_run_at TEXT, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_gaps (
    id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES topics(id), entity_id TEXT NOT NULL REFERENCES entities(id),
    field_id TEXT REFERENCES field_definitions(id), element_id TEXT REFERENCES dimension_elements(id),
    state TEXT NOT NULL CHECK(state IN ('not_searched','searched_not_found','explicitly_undisclosed',
        'not_applicable','conflicting')),
    detail TEXT NOT NULL DEFAULT '', last_checked_at TEXT, next_check_at TEXT,
    task_id TEXT REFERENCES maintenance_tasks(id), evidence_claim_id TEXT REFERENCES claims(id),
    CHECK(field_id IS NOT NULL OR element_id IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS review_decisions (
    id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES claims(id), reviewer TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('accept','dispute','withdraw','unlock')),
    reason TEXT NOT NULL, decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claim_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, claim_id TEXT NOT NULL REFERENCES claims(id),
    previous_status TEXT, new_status TEXT NOT NULL, changed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS entities_kind_name ON entities(kind,canonical_name);
CREATE INDEX IF NOT EXISTS topic_entities_reverse ON topic_entities(entity_id,topic_id);
CREATE INDEX IF NOT EXISTS claims_subject_topic ON claims(topic_id,subject_id,status);
CREATE INDEX IF NOT EXISTS claims_run ON claims(extraction_run_id);
CREATE INDEX IF NOT EXISTS relation_object ON entity_relations(object_id,relation_type_id);
CREATE INDEX IF NOT EXISTS field_lookup ON field_values(field_id);
CREATE INDEX IF NOT EXISTS metric_lookup ON metric_observations(metric_id);
CREATE INDEX IF NOT EXISTS participation_object ON participations(object_id,role_id);
CREATE INDEX IF NOT EXISTS evidence_segment ON claim_evidence(segment_id);
CREATE INDEX IF NOT EXISTS maintenance_queue ON maintenance_tasks(status,priority,next_run_at);

CREATE VIEW IF NOT EXISTS accepted_relations AS
SELECT c.id,c.topic_id,c.subject_id,r.relation_type_id,r.object_id,r.function_field_id,
       c.valid_from,c.valid_to,c.context_id,c.scope_product_id
FROM claims c JOIN entity_relations r ON r.claim_id=c.id WHERE c.status='accepted';
CREATE VIEW IF NOT EXISTS accepted_fields AS
SELECT c.id,c.topic_id,c.subject_id,v.field_id,v.row_key,v.value_json,v.qualifiers_json,
       c.valid_from,c.valid_to,c.scope_product_id
FROM claims c JOIN field_values v ON v.claim_id=c.id WHERE c.status='accepted';
CREATE VIEW IF NOT EXISTS accepted_metrics AS
SELECT c.id,c.topic_id,c.subject_id,m.*,c.context_id,c.scope_product_id,c.valid_from,c.valid_to
FROM claims c JOIN metric_observations m ON m.claim_id=c.id WHERE c.status='accepted';
CREATE VIEW IF NOT EXISTS topic_dimension_coverage AS
SELECT t.id topic_id,d.id dimension_id,d.name,
       (SELECT count(*) FROM dimension_elements e WHERE e.dimension_id=d.id) element_count,
       (SELECT count(*) FROM field_storage_mappings f JOIN dimension_elements e ON e.id=f.element_id
        WHERE e.dimension_id=d.id) registered_field_count
FROM topics t CROSS JOIN dimensions d;

CREATE TRIGGER IF NOT EXISTS immutable_source_revision BEFORE UPDATE ON source_revisions
BEGIN SELECT RAISE(ABORT,'source revisions are immutable; append a new revision'); END;
CREATE TRIGGER IF NOT EXISTS immutable_source_segment BEFORE UPDATE ON source_segments
BEGIN SELECT RAISE(ABORT,'source segments are immutable; append a new revision'); END;
CREATE TRIGGER IF NOT EXISTS immutable_test_context BEFORE UPDATE ON test_contexts
BEGIN SELECT RAISE(ABORT,'test contexts are immutable; append a new context'); END;
CREATE TRIGGER IF NOT EXISTS immutable_entity_name BEFORE UPDATE ON entity_names
BEGIN SELECT RAISE(ABORT,'names are immutable; register a new name with its validity'); END;
CREATE TRIGGER IF NOT EXISTS immutable_entity_identifier BEFORE UPDATE ON entity_identifiers
BEGIN SELECT RAISE(ABORT,'identifiers are immutable; append a separate identifier'); END;
CREATE TRIGGER IF NOT EXISTS immutable_review_decision BEFORE UPDATE ON review_decisions
BEGIN SELECT RAISE(ABORT,'review history is immutable; append a new decision'); END;
CREATE TRIGGER IF NOT EXISTS quote_must_exist BEFORE INSERT ON claim_evidence
WHEN length(trim(NEW.quote))=0 OR NOT EXISTS (
    SELECT 1 FROM source_segments s WHERE s.id=NEW.segment_id AND instr(s.text_content,NEW.quote)>0
)
BEGIN SELECT RAISE(ABORT,'evidence quote must be a continuous original segment substring'); END;
CREATE TRIGGER IF NOT EXISTS evidence_is_immutable BEFORE UPDATE ON claim_evidence
BEGIN SELECT RAISE(ABORT,'evidence is immutable; append a separate evidence record'); END;
CREATE TRIGGER IF NOT EXISTS claims_start_as_candidates BEFORE INSERT ON claims
WHEN NEW.status<>'candidate'
BEGIN SELECT RAISE(ABORT,'insert candidate, attach details and evidence, then accept'); END;
CREATE TRIGGER IF NOT EXISTS claims_require_topic_subject BEFORE INSERT ON claims
WHEN NOT EXISTS(SELECT 1 FROM topic_entities WHERE topic_id=NEW.topic_id AND entity_id=NEW.subject_id)
BEGIN SELECT RAISE(ABORT,'claim subject must belong to the claim topic'); END;
CREATE TRIGGER IF NOT EXISTS claims_run_topic_matches BEFORE INSERT ON claims
WHEN NEW.extraction_run_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM extraction_runs WHERE id=NEW.extraction_run_id AND topic_id=NEW.topic_id
)
BEGIN SELECT RAISE(ABORT,'extraction run and claim topics must match'); END;
CREATE TRIGGER IF NOT EXISTS claims_product_scope_matches BEFORE INSERT ON claims
WHEN NEW.scope_product_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM topic_entities WHERE topic_id=NEW.topic_id AND entity_id=NEW.scope_product_id
)
BEGIN SELECT RAISE(ABORT,'scope product must belong to the claim topic'); END;
CREATE TRIGGER IF NOT EXISTS composition_types_must_match BEFORE INSERT ON composition_items
WHEN NOT EXISTS (
    SELECT 1 FROM claims c JOIN entities s ON s.id=c.subject_id
    JOIN entities o ON o.id=NEW.component_id
    WHERE c.id=NEW.claim_id AND s.kind IN ('technology','route','product','component','process')
    AND o.kind IN ('technology','route','product','component','material','process','reference')
    AND EXISTS(SELECT 1 FROM topic_entities t WHERE t.topic_id=c.topic_id AND t.entity_id=o.id)
)
BEGIN SELECT RAISE(ABORT,'composition requires technical objects in the same topic'); END;
CREATE TRIGGER IF NOT EXISTS interface_types_must_match BEFORE INSERT ON interface_connections
WHEN NOT EXISTS (
    SELECT 1 FROM claims c JOIN entities s ON s.id=c.subject_id
    WHERE c.id=NEW.claim_id AND s.kind IN ('technology','route','product','component','process')
    AND EXISTS(SELECT 1 FROM topic_entities t
        WHERE t.topic_id=c.topic_id AND t.entity_id=NEW.interface_id)
)
BEGIN SELECT RAISE(ABORT,'interface requires a technical subject and a topic member'); END;
CREATE TRIGGER IF NOT EXISTS claims_identity_is_immutable BEFORE UPDATE ON claims
WHEN NEW.topic_id<>OLD.topic_id OR NEW.subject_id<>OLD.subject_id OR NEW.claim_kind<>OLD.claim_kind
 OR NEW.assertion_type<>OLD.assertion_type OR NEW.context_id IS NOT OLD.context_id
 OR NEW.scope_product_id IS NOT OLD.scope_product_id OR NEW.scope_note<>OLD.scope_note
 OR NEW.valid_from IS NOT OLD.valid_from OR NEW.valid_to IS NOT OLD.valid_to
 OR NEW.extraction_run_id IS NOT OLD.extraction_run_id OR NEW.recorded_at<>OLD.recorded_at
 OR NEW.supersedes_id IS NOT OLD.supersedes_id OR NEW.id<>OLD.id
BEGIN SELECT RAISE(ABORT,'fact identity/time/context is immutable; append a new claim'); END;
CREATE TRIGGER IF NOT EXISTS accepted_requires_evidence BEFORE UPDATE OF status ON claims
WHEN NEW.status='accepted' AND NOT EXISTS (
    SELECT 1 FROM claim_evidence WHERE claim_id=NEW.id AND stance='supports'
)
BEGIN SELECT RAISE(ABORT,'acceptance requires supporting original evidence'); END;
CREATE TRIGGER IF NOT EXISTS model_suggestions_cannot_be_accepted BEFORE UPDATE OF status ON claims
WHEN NEW.status='accepted' AND NEW.assertion_type='model_suggestion'
BEGIN SELECT RAISE(ABORT,'model suggestions require a new evidence-based claim'); END;
CREATE TRIGGER IF NOT EXISTS accepted_evidence_cannot_be_removed BEFORE DELETE ON claim_evidence
WHEN OLD.stance='supports' AND (SELECT status FROM claims WHERE id=OLD.claim_id)='accepted'
 AND (SELECT count(*) FROM claim_evidence WHERE claim_id=OLD.claim_id AND stance='supports')=1
BEGIN SELECT RAISE(ABORT,'accepted claim must retain supporting evidence'); END;
CREATE TRIGGER IF NOT EXISTS subject_membership_cannot_be_removed BEFORE DELETE ON topic_entities
WHEN EXISTS(SELECT 1 FROM claims WHERE topic_id=OLD.topic_id AND subject_id=OLD.entity_id)
BEGIN SELECT RAISE(ABORT,'topic membership is referenced by claims'); END;
CREATE TRIGGER IF NOT EXISTS claims_history_insert AFTER INSERT ON claims
BEGIN INSERT INTO claim_history(claim_id,new_status,changed_at)
VALUES(NEW.id,NEW.status,strftime('%Y-%m-%dT%H:%M:%fZ','now')); END;
CREATE TRIGGER IF NOT EXISTS claims_history_update AFTER UPDATE OF status ON claims
WHEN NEW.status<>OLD.status
BEGIN INSERT INTO claim_history(claim_id,previous_status,new_status,changed_at)
VALUES(NEW.id,OLD.status,NEW.status,strftime('%Y-%m-%dT%H:%M:%fZ','now')); END;
