-- Read-only examples. All business rows are initially empty.
-- Change topic:anduril to a later topic ID to reuse these queries.

-- The seven image dimensions and all 48 small boxes.
SELECT d.id, d.name AS dimension, e.name AS element, e.primary_table, e.storage_rule
FROM dimensions d JOIN dimension_elements e ON e.dimension_id=d.id
ORDER BY d.id,e.ordinal;

-- Legacy fields and their relational destinations.
SELECT f.id,f.label,f.subject_kind,f.value_type,d.name AS dimension,
       e.name AS element,m.target_table,m.storage_rule
FROM field_definitions f JOIN field_storage_mappings m ON m.field_id=f.id
JOIN dimension_elements e ON e.id=m.element_id JOIN dimensions d ON d.id=e.dimension_id
ORDER BY d.id,f.subject_kind,f.id;

-- Product -> adopted technology -> route -> researching team.
SELECT product.canonical_name AS product,tech.canonical_name AS technology,
       route.canonical_name AS route,team.canonical_name AS researching_team
FROM accepted_relations adopted
JOIN entities product ON product.id=adopted.subject_id
JOIN entities tech ON tech.id=adopted.object_id
JOIN accepted_relations routed ON routed.subject_id=tech.id
    AND routed.relation_type_id='has_route' AND routed.topic_id=adopted.topic_id
JOIN entities route ON route.id=routed.object_id
LEFT JOIN (
    SELECT c.subject_id,p.object_id,c.topic_id FROM claims c
    JOIN participations p ON p.claim_id=c.id
    WHERE c.status='accepted' AND p.role_id='researcher'
) research ON research.object_id=route.id AND research.topic_id=adopted.topic_id
LEFT JOIN entities team ON team.id=research.subject_id
WHERE adopted.topic_id='topic:anduril' AND adopted.relation_type_id='uses_technology';

-- Observe all supported values, without choosing the latest or largest.
SELECT e.canonical_name AS subject,m.canonical_name AS metric,o.raw_value,o.raw_unit,
       o.statistic,o.value_nature,ctx.conditions_json,c.valid_from,c.valid_to,
       c.scope_product_id,evidence.quote,doc.canonical_uri
FROM claims c JOIN metric_observations o ON o.claim_id=c.id
JOIN entities e ON e.id=c.subject_id JOIN entities m ON m.id=o.metric_id
JOIN test_contexts ctx ON ctx.id=c.context_id
JOIN claim_evidence evidence ON evidence.claim_id=c.id AND evidence.stance='supports'
JOIN source_segments s ON s.id=evidence.segment_id
JOIN source_revisions r ON r.id=s.revision_id JOIN source_documents doc ON doc.id=r.document_id
WHERE c.topic_id='topic:anduril' AND c.status='accepted';

-- Application cases, products, users and operators (one row per participation).
SELECT app.canonical_name AS application,actor.canonical_name AS participant,
       p.role_id,c.valid_from,c.valid_to
FROM claims c JOIN participations p ON p.claim_id=c.id
JOIN entities app ON app.id=p.object_id JOIN entities actor ON actor.id=c.subject_id
WHERE c.topic_id='topic:anduril' AND c.status='accepted' AND app.kind='application';

-- Gaps and queued maintenance work.
SELECT t.title,e.canonical_name,g.state,g.detail,g.next_check_at,m.status AS task_status
FROM knowledge_gaps g JOIN topics t ON t.id=g.topic_id JOIN entities e ON e.id=g.entity_id
LEFT JOIN maintenance_tasks m ON m.id=g.task_id WHERE g.topic_id='topic:anduril';
