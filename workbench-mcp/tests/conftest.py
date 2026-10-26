"""Fleet-wide test fixtures for the workbench suite.

D18 (2026-09-13): the SHIPPED default allowlist no longer contains
python3/pip/pip3.  Most behavioral tests exercise command mechanics that
historically ran under the old default, so they run against the pre-D18
allowlist explicitly (operator-opted semantics) via this autouse fixture.
The DEFAULT itself is pinned separately by tests/test_d18.py.
"""

import pytest

PRE_D18_ALLOWLIST = "python3,pip,pip3,ls,cat,head,tail,grep,find,wc,du,df,mkdir,touch,cp,mv,tar,git,diff,sort,uniq"


@pytest.fixture(autouse=True)
def _pre_d18_allowlist(monkeypatch):
    monkeypatch.setenv("WORKBENCH_EXEC_ALLOWLIST", PRE_D18_ALLOWLIST)
