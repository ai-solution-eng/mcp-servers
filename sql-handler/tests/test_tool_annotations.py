"""Every tool declares honest MCP annotations (the 2026 hint set).

Clients that default pessimistically treat an ABSENT `destructiveHint` as
"maybe destructive", so a plain SELECT must be explicitly read-only AND NOT
destructive. These tests pin the posture table and the serialized tools/list
shape (camelCase hints), so a new tool without annotations — or a read-only
tool flipped to destructive — fails here.

Run: python -m pytest tests/test_tool_annotations.py -v
"""

from sqlhandler import server

#: tool -> (read_only, destructive, idempotent, open_world)
EXPECTED = {
    # identity / admin plane (in-process state only)
    "whoami": (True, False, True, False),
    "admin_grants": (True, False, True, False),
    "admin_policy_set": (False, True, True, False),
    "admin_key_mint": (False, False, False, False),
    "admin_key_revoke": (False, True, True, False),
    # catalog + query readers (they reach the lake / attached databases)
    "list_tables": (True, False, True, True),
    "describe_table": (True, False, True, True),
    "profile_table": (True, False, True, True),
    "search_tables": (True, False, True, True),
    "run_sql": (True, False, True, True),
    "scan_table": (True, False, True, True),
    "column_stats": (True, False, True, True),
    "sample_rows": (True, False, True, True),
    "explain_query": (True, False, True, True),
    "ask_data": (True, False, True, True),
    # async jobs
    "query_submit": (True, False, False, True),
    "query_status": (True, False, True, False),
    "query_result": (True, False, False, False),
    "query_cancel": (False, False, True, False),
    # saved queries (local store)
    "query_save": (False, False, False, False),
    "query_list": (True, False, True, False),
    "query_delete": (False, True, True, False),
    "query_saved": (True, False, True, True),
}


def test_every_tool_declares_all_four_hints():
    missing = [t.name for t in server._TOOLS if t.annotations is None]
    assert missing == [], f"tools without annotations: {missing}"
    for tool in server._TOOLS:
        ann = tool.annotations
        assert ann.read_only_hint is not None, tool.name
        assert ann.destructive_hint is not None, tool.name
        assert ann.idempotent_hint is not None, tool.name
        assert ann.open_world_hint is not None, tool.name


def test_posture_table_matches_every_registered_tool():
    actual = {t.name for t in server._TOOLS}
    assert actual == set(EXPECTED), f"unpinned tools: {sorted(actual ^ set(EXPECTED))}"
    for tool in server._TOOLS:
        ann = tool.annotations
        got = (ann.read_only_hint, ann.destructive_hint, ann.idempotent_hint, ann.open_world_hint)
        assert got == EXPECTED[tool.name], f"{tool.name}: {got} != {EXPECTED[tool.name]}"


def test_no_read_only_tool_is_presented_as_destructive():
    """The whole point of the change: pessimistic clients must not see a
    read-only tool as destructive."""
    offenders = [t.name for t in server._TOOLS if t.annotations.read_only_hint and t.annotations.destructive_hint]
    assert offenders == [], f"read-only tools marked destructive: {offenders}"


def test_mutations_are_not_marked_read_only():
    """The inverse honesty check: a state-changing tool must not claim
    read-only, or a client may run it without confirmation."""
    for name in (
        "admin_policy_set",
        "admin_key_mint",
        "admin_key_revoke",
        "query_save",
        "query_delete",
        "query_cancel",
    ):
        tool = next(t for t in server._TOOLS if t.name == name)
        assert tool.annotations.read_only_hint is False, name


def test_tools_list_serializes_camel_case_hints():
    """tools/list is what clients see: camelCase keys, booleans, per tool."""
    dumped = [t.model_dump(by_alias=True, exclude_none=True) for t in server._TOOLS]
    for payload in dumped:
        assert set(payload["annotations"]) == {
            "readOnlyHint",
            "destructiveHint",
            "idempotentHint",
            "openWorldHint",
        }
        assert isinstance(payload["annotations"]["readOnlyHint"], bool)
    # and the existing inspector contract is untouched
    assert set(server.mcp_tool_specs()[0]) == {"name", "description", "inputSchema"}
