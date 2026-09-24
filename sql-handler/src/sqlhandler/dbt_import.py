"""dbt manifest.json → semantic-catalog importer.

Turns the ``target/manifest.json`` artifact of ``dbt compile`` / ``dbt build``
into semantic-catalog ``tables`` content: the same ``{"tables": {key: entry}}``
shape the engine loads from ``SQLHANDLER_CATALOG``, the chart's
``semanticCatalog.*`` values, or the upload store behind
``POST /api/semantic-catalog``. Nothing here queries dbt, needs dbt installed
(parsing is stdlib ``json``), or touches the engine — the module is a pure
manifest → catalog mapping so it is unit-testable in isolation and usable from
the REST layer, scripts, or a future tool.

What is consumed from the manifest (dbt manifest v1):
  * ``nodes`` with ``resource_type`` ``model`` / ``seed`` / ``snapshot`` —
    tests, analyses, exposures and operations are ignored (they document
    assertions and ad-hoc queries, not tables);
  * ``name`` / ``alias`` / ``schema`` / ``relation_name`` / ``database`` —
    resolved to the catalog key the ENGINE actually lists (see below);
  * ``description`` (fallback: ``meta.sqlhandler.description``) and
    ``columns.{col}.description`` / ``.dtype``;
  * ``meta.sqlhandler.virtual`` + ``compiled_sql`` — virtual-table export,
    double-gated (node meta AND request-level ``allow_virtual``);
  * ``meta.sqlhandler.hide`` — the node is omitted from the output entirely;
  * ``meta.sqlhandler.aliases`` — extra search terms for ``search_tables``.

Catalog key resolution (the one genuinely fiddly part): the engine matches
catalog keys against provider tables in ``path → qualified_name → name``
order, so the best key is the table's ``<schema>/<name>`` path (or the bare
name for a root-level table). dbt names carry dots (``analytics.stg_orders``),
so the fallback ladder below is tried in order and ``alias_map`` (explicit
``dbt_name → catalog_name``) wins over every generated form.

Merge semantics (imported_from marker): the schema's
``_validate_catalog_entry`` preserves unknown keys, so an entry may carry
``meta: {imported_from: "dbt", manifest_fields: [...]}`` directly — no
sidecar needed. Applying merges node-by-node into the effective catalog:
an existing key is overwritten ONLY when it is a previous dbt import
(marker present) or ``force_overwrite`` is set; everything else is a
proposed addition in the returned catalog until the operator applies it.
"""

from __future__ import annotations

import base64
import json
import re

__all__ = [
    "META_PREFIX",
    "DbtImportError",
    "apply_import",
    "import_dbt_manifest",
]

# dbt meta namespace: one dict under meta.sqlhandler, never flat keys.
META_PREFIX = "sqlhandler"

# A catalog entry meta marker recording WHERE an entry came from. The merge
# rule keys on it: a previous dbt import may be re-imported over; anything
# else (hand-written YAML, another tool) is never silently replaced.
_IMPORTED_FROM_DBT = "dbt"

# The engine registers virtual tables ONLY from bare-identifier catalog keys
# (engine.py _VIRTUAL_NAME_RE): a definition under "analytics/vw_big" is
# silently skipped with a warning. Virtual entries therefore always take the
# model's bare dbt name (alias-mapped when alias_map says so).
_VIRTUAL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DbtImportError(ValueError):
    """User-input error from a dbt manifest import (→ HTTP 400)."""


# ---------------------------------------------------------------------------
# manifest ingestion
# ---------------------------------------------------------------------------


def _decode_manifest(body: object) -> dict:
    """Accept an inline manifest dict, raw JSON text, or base64 of that JSON."""
    if isinstance(body, dict):
        manifest = body
    elif isinstance(body, (str, bytes)):
        if isinstance(body, bytes):
            try:
                body = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise DbtImportError(f"manifest bytes are not UTF-8: {exc}") from exc
        text = body.strip()
        if not text:
            raise DbtImportError("manifest is empty")
        try:
            manifest = json.loads(text)
        except json.JSONDecodeError as exc:
            # Not JSON — only remaining shape is base64 of a JSON manifest.
            try:
                decoded = base64.b64decode(text, validate=True).decode("utf-8")
            except Exception:
                raise DbtImportError(
                    f"manifest is not valid JSON or base64 (JSON error: {exc})"
                ) from None
            try:
                manifest = json.loads(decoded)
            except json.JSONDecodeError as exc:
                raise DbtImportError(f"base64-decoded manifest is not valid JSON: {exc}") from None
    else:
        raise DbtImportError("manifest must be an object, a JSON string, or base64 text")
    if not isinstance(manifest, dict):
        raise DbtImportError("manifest must be a JSON object")
    return manifest


def _meta(node: dict) -> dict:
    meta = node.get("meta")
    return meta.get(META_PREFIX) if isinstance(meta, dict) and isinstance(meta.get(META_PREFIX), dict) else {}


def _description(node: dict, meta: dict, name: str) -> str | None:
    desc = node.get("description")
    if not isinstance(desc, str) or not desc.strip():
        desc = meta.get("description")
    return desc if isinstance(desc, str) and desc.strip() else None


def _schema_name(node: dict) -> str:
    schema = node.get("schema")
    return schema.strip() if isinstance(schema, str) and schema.strip() else ""


def _dotted(name: str) -> str:
    return name.replace(".", "_")


def _generated_keys(node: dict, name: str, alias: str) -> list[str]:
    """Catalog keys for one node, best first (see the module docstring).

    dbt always puts models in a schema, so ``schema/name`` leads. Root-level
    DATA files (no parent directory) surface with schema ``"default"`` while
    their path is bare — dbt's schema would generate ``default/<name>``,
    which matches nothing, so the bare name comes second for those. For
    nested tables the bare name is a deliberate third: the engine resolves
    it when unambiguous, and it keeps working if the table is later moved
    under a different schema (docs/semantic-catalog.md, table-key forms).
    """
    schema = _schema_name(node)
    clean_alias = _dotted(alias) if alias != name else ""
    keys: list[str] = []
    if schema and schema != "default":
        keys.append(f"{schema}/{name}")
    keys.append(name)
    if schema and schema != "default":
        keys.append(f"{schema}/{clean_alias}" if clean_alias else name)
        if clean_alias and clean_alias != name:
            keys.append(f"{clean_alias}/{name}")
    if clean_alias and clean_alias != name:
        keys.append(clean_alias)
    return keys


def _importable_nodes(manifest: dict, source_filter: str | None, warnings: list[str]) -> list[tuple[str, dict]]:
    """The manifest's table-bearing nodes: models/seeds/snapshots only.

    Ephemeral models are skipped by default (they are intermediate SQL with
    no relation) unless another node's ``depends_on`` forces one in — an
    ephemeral dependency that never lands in the catalog would silently
    break every virtual definition built on it. Tests/analyses/exposures/
    operations never qualify. ``source_filter`` keeps only nodes whose
    ``<database>.<schema>.<name>`` relation_name (or bare name) contains it.
    """
    nodes = manifest.get("nodes")
    if not isinstance(nodes, dict):
        raise DbtImportError("manifest is missing a 'nodes' mapping (is this a dbt manifest.json?)")
    wanted = ("model", "seed", "snapshot")
    included: dict[str, dict] = {}
    ephemeral: list[str] = []
    for uid, node in nodes.items():
        if not isinstance(node, dict):
            continue
        if node.get("resource_type") not in wanted:
            continue
        if source_filter:
            rel = node.get("relation_name") or ""
            if source_filter.lower() not in str(rel).lower() and source_filter.lower() not in str(uid).lower():
                continue
        if node.get("config", {}).get("materialized") == "ephemeral":
            ephemeral.append(uid)
            continue
        included[uid] = node
    if ephemeral:
        # Pull ephemeral models back in ONLY when an included node depends on
        # them — a virtual definition referencing them would otherwise break.
        needed = {dep for node in included.values() for dep in node.get("depends_on", {}).get("nodes", []) if isinstance(dep, str)}
        for uid in ephemeral:
            if uid in needed:
                node = nodes[uid]
                if source_filter:
                    rel = node.get("relation_name") or ""
                    if source_filter.lower() not in str(rel).lower() and source_filter.lower() not in str(uid).lower():
                        continue
                included[uid] = node
                warnings.append(f"{uid.split('.')[-1]}: ephemeral but imported — an included model depends on it")
    return sorted(included.items())


# ---------------------------------------------------------------------------
# node → catalog entry
# ---------------------------------------------------------------------------


def _columns_for(node: dict) -> dict[str, str] | None:
    """dbt columns → the catalog's documented shape (``name → description``).

    The catalog's ``columns`` mapping carries STRING descriptions only (a
    plain ``str(col)`` per column — docs/semantic-catalog.md "columns: Map of
    column name → one-line description"). dbt's per-column ``dtype`` has no
    home in that shape, so it degrades to a warning: if a model documents a
    dtype but no description, that column is imported with a one-line
    "<dtype> column" placeholder rather than dropped — the dtype is
    information the operator would otherwise lose.
    """
    columns = node.get("columns")
    if not isinstance(columns, dict) or not columns:
        return None
    out: dict[str, str] = {}
    skipped: list[str] = []
    for col_name, col in columns.items():
        if not isinstance(col, dict):
            continue
        doc = col.get("description")
        if isinstance(doc, str) and doc.strip():
            out[str(col_name)] = doc.strip()
        elif isinstance(col.get("dtype"), str) and col["dtype"].strip():
            out[str(col_name)] = f"{col['dtype'].strip()} column"
            skipped.append(str(col_name))
    if skipped:
        skipped.sort()
    return out or None


def _definition_for(node: dict, meta: dict, virtual_enabled: bool, warnings: list[str], skipped: dict[str, str]) -> str | None:
    """The virtual-table definition, honoring the double gate.

    Returns None (no definition) for anything not both requested and safe;
    reasons for dropped candidates are recorded in ``skipped`` so the caller
    can surface counts instead of failing the whole import.
    """
    if not meta.get("virtual"):
        return None
    name = str(node.get("name") or "?")
    if not virtual_enabled:
        skipped[name] = "meta.sqlhandler.virtual set but the request did not pass allow_virtual: true"
        return None
    sql = node.get("compiled_sql")
    if not isinstance(sql, str) or not sql.strip():
        skipped[name] = "meta.sqlhandler.virtual set but the node has no compiled_sql (run `dbt compile` first)"
        return None
    first = sql.lstrip()[0:1].upper()
    if first not in ("S", "W"):
        skipped[name] = (
            f"meta.sqlhandler.virtual set but compiled_sql is not a read-only SELECT/WITH "
            f"(starts with {first!r}) — INSERT/DDL never becomes a virtual table"
        )
        return None
    return sql.strip()


def _entry_for(node: dict, warnings: list[str]) -> dict:
    """One node's documentation entry, minus the definition (added later)."""
    name = str(node.get("name") or "?")
    meta = _meta(node)
    entry: dict = {}
    desc = _description(node, meta, name)
    if desc:
        entry["description"] = desc
    columns = _columns_for(node)
    if columns:
        entry["columns"] = columns
    raw_aliases = meta.get("aliases")
    if isinstance(raw_aliases, list) and all(isinstance(a, str) and a.strip() for a in raw_aliases) and raw_aliases:
        entry["aliases"] = [a.strip() for a in raw_aliases]
    elif raw_aliases not in (None, [],) and not isinstance(raw_aliases, list):
        warnings.append(f"{name}: meta.sqlhandler.aliases ignored — must be a list of strings")
    return entry


# ---------------------------------------------------------------------------
# public surface
# ---------------------------------------------------------------------------


def import_dbt_manifest(
    manifest: object,
    *,
    allow_virtual: bool = False,
    source_filter: str | None = None,
    alias_map: dict | None = None,
) -> dict:
    """Import a dbt ``manifest.json`` into semantic-catalog ``tables`` content.

    Args:
        manifest: the parsed manifest dict, raw JSON text, or base64 of that
            JSON (the ``target/manifest.json`` artifact of ``dbt compile``).
        allow_virtual: request-level gate for virtual-table generation. Both
            this AND the node's ``meta.sqlhandler.virtual: true`` must be set
            for a definition to be emitted (belt and suspenders — a global
            flag alone must never turn on virtual tables).
        source_filter: substring match (case-insensitive) on the node's
            ``relation_name`` or unique_id; None imports everything.
        alias_map: explicit ``dbt node name → catalog table key`` overrides;
            wins over every generated key.

    Returns ``{"catalog": {"tables": {...}}, "imported": n, "virtuals": n,
    "skipped": n, "warnings": [...]}``. The catalog is a PROPOSED merged view
    (see :func:`apply_import`) — nothing is written by this function.
    """
    manifest = _decode_manifest(manifest)
    if alias_map is not None and not isinstance(alias_map, dict):
        raise DbtImportError("alias_map must map dbt node names to catalog table keys")
    if alias_map:
        alias_map = {str(k): str(v) for k, v in alias_map.items()}
    warnings: list[str] = []
    skipped: dict[str, str] = {}
    tables: dict[str, dict] = {}
    for uid, node in _importable_nodes(manifest, source_filter, warnings):
        name = str(node.get("name") or "")
        meta = _meta(node)
        if meta.get("hide"):
            skipped[name or uid] = "hidden via meta.sqlhandler.hide"
            continue
        entry = _entry_for(node, warnings)
        definition = _definition_for(node, meta, allow_virtual, warnings, skipped)
        if not entry and not definition:
            # Nothing to say and nothing to run: importing an empty entry
            # would overwrite a richer hand-written one with nothing.
            skipped[name or uid] = "no description and no columns documented"
            continue
        key = alias_map.get(name) if alias_map else None
        if definition and not (key and _VIRTUAL_NAME_RE.match(key)):
            # The engine registers virtual tables ONLY from bare-identifier
            # catalog keys — a definition under "analytics/vw_big" would be
            # skipped with a warning at load. Virtual entries always take the
            # model's bare dbt name (the alias_map override is kept when it
            # is itself a clean identifier).
            key = name if _VIRTUAL_NAME_RE.match(name) else _dotted(name)
        elif not key:
            alias = str(node.get("alias") or name)
            keys = _generated_keys(node, name, alias)
            for candidate in keys:
                if candidate not in tables:
                    key = candidate
                    break
            if key in tables:  # pragma: no cover - the loop above avoids this
                key = keys[-1]
        if key in tables:
            warnings.append(f"{name}: two nodes resolve to catalog key {key!r}; the later ({uid}) wins")
        if definition:
            entry["definition"] = definition
        entry["meta"] = {"imported_from": _IMPORTED_FROM_DBT, "manifest_node": uid}
        tables[key] = entry
    # `skipped` doubles as the virtual-refusal reason ledger — collapsed into
    # warnings so a refused virtual table is never a silent no-op.
    for skipped_name, reason in sorted(skipped.items()):
        warnings.append(f"{skipped_name}: {reason}")
    return {
        "catalog": {"tables": tables},
        "imported": len(tables),
        "virtuals": sum(1 for entry in tables.values() if entry.get("definition")),
        "skipped": len(skipped),
        "warnings": warnings,
    }


def _validated_tables(catalog: dict) -> dict:
    """Type-check the importer's own output the way the upload store would.

    Mirrors ``SqlEngine._validate_catalog_entry`` (string description, list
    aliases, string→string columns) so an import that somehow produces a
    wrong-typed value is caught HERE, at the boundary, instead of surfacing
    as a confusing 400 from the store write.
    """
    tables = catalog.get("tables")
    if not isinstance(tables, dict):
        raise DbtImportError("import produced no 'tables' mapping (internal error)")  # pragma: no cover
    for key, entry in tables.items():
        if not isinstance(entry, dict):
            raise DbtImportError(f"tables[{key!r}] must be a mapping of documentation fields")
        desc = entry.get("description")
        if desc is not None and not isinstance(desc, str):
            raise DbtImportError(f"tables[{key!r}]: 'description' must be a string")
        aliases = entry.get("aliases")
        if aliases is not None and (not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases)):
            raise DbtImportError(f"tables[{key!r}]: 'aliases' must be a list of strings")
        columns = entry.get("columns")
        if columns is not None and (
            not isinstance(columns, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in columns.items())
        ):
            raise DbtImportError(f"tables[{key!r}]: 'columns' must map column names to string descriptions")
        definition = entry.get("definition")
        if definition is not None and (not isinstance(definition, str) or not definition.strip()):
            raise DbtImportError(f"tables[{key!r}]: 'definition' must be a non-empty SQL string")
    return tables


def apply_import(engine, import_result: dict, *, force_overwrite: bool = False) -> dict:
    """Apply an importer result through the EXISTING validated upload store.

    Reuses ``SqlEngine.set_catalog_text`` (validation, atomic write, hot
    reload, precedence over the configured file) by submitting the FULL
    merged catalog as text — the same canonical-JSON on-disk format the
    upload API writes.

    Merge rule (node-by-node, no sidecar needed — the schema preserves
    unknown keys): an existing catalog key is overwritten when
      * it carries the dbt-import marker (``meta.imported_from == "dbt"``,
        set by a previous run of this importer), so re-imports converge, OR
      * ``force_overwrite`` is true (explicit "dbt is the source of truth").
    Everything else (hand-written entries, other tools' imports) survives
    and appears only as a proposed addition in the returned catalog.

    Raises ``ValueError`` when uploads are disabled
    (``SQLHANDLER_CATALOG_UPLOAD=0``) — the same refusal the upload API
    gives — and propagates ``OSError`` (unwritable store) to the caller.
    """
    if not engine.catalog_uploads_enabled:
        raise ValueError("Semantic-catalog upload is disabled (SQLHANDLER_CATALOG_UPLOAD=0).")
    tables = _validated_tables(import_result.get("catalog") or {})
    try:
        current = dict(engine._catalog())
    except Exception:
        current = {}
    merged = dict(current)
    overwritten = 0
    for key, entry in tables.items():
        existing = merged.get(key)
        if isinstance(existing, dict):
            prior_meta = existing.get("meta")
            prior = prior_meta.get("imported_from") if isinstance(prior_meta, dict) else None
            if prior == _IMPORTED_FROM_DBT or force_overwrite:
                merged[key] = entry
                overwritten += 1
                continue
            import_result.setdefault("warnings", []).append(
                f"{key}: existing entry kept (hand-written or not from dbt) — pass force_overwrite to replace it"
            )
            continue
        merged[key] = entry
    result = engine.set_catalog_text(json.dumps({"tables": merged}, ensure_ascii=False))
    return {
        "applied": len(tables),
        "overwritten": overwritten,
        "tables": result.get("tables", len(merged)),
        "path": result.get("path"),
        "merged_total": len(merged),
    }
