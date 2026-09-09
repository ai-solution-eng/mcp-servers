import json, urllib.request, ssl
tok = open('.bearer').read().strip()
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
BASE = "https://ezpresto.pcai-se-ai-application.hst.rdlabs.hpecorp.net"

def q(sql, quiet=False):
    req = urllib.request.Request(BASE + "/v1/statement", data=sql.encode(), headers={
        "Authorization": f"Bearer {tok}", "X-Presto-User": "andrew-bydlon", "Content-Type": "text/plain"})
    rows = []
    for _ in range(200):
        with urllib.request.urlopen(req, context=ctx, timeout=180) as r:
            body = json.loads(r.read())
        if body.get("error"):
            raise RuntimeError(body["error"].get("message"))
        chunk = body.get("data") or []
        for d in chunk:
            if isinstance(d, dict) and "data" in d: rows.extend(d["data"])
            elif isinstance(d, list): rows.append(d)
        if body["stats"]["state"] in ("FINISHED", "FAILED", "CANCELED"):
            return rows
        req = urllib.request.Request(body["nextUri"], headers={"Authorization": f"Bearer {tok}"})
    raise RuntimeError("too many polls")
