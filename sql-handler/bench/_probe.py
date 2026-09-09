import json, urllib.request, ssl, sys
tok = open('.bearer').read().strip()
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
BASE = "https://ezpresto.pcai-se-ai-application.hst.rdlabs.hpecorp.net"

def q(sql):
    url, req, rows = BASE + "/v1/statement", None, []
    req = urllib.request.Request(url, data=sql.encode(), headers={
        "Authorization": f"Bearer {tok}", "X-Presto-User": "andrew-bydlon", "Content-Type": "text/plain"})
    for _ in range(200):
        with urllib.request.urlopen(req, context=ctx, timeout=120) as r:
            body = json.loads(r.read())
        if body.get("error"):
            raise RuntimeError(body["error"].get("message"))
        for d in body.get("data", []) or []:
            rows.extend(d["data"] if isinstance(d, dict) and "data" in d else (d if isinstance(d, list) else [d]))
        if body["stats"]["state"] in ("FINISHED", "FAILED", "CANCELED"):
            return rows
        nu = body.get("nextUri")
        req = urllib.request.Request(nu, headers={"Authorization": f"Bearer {tok}"})
    raise RuntimeError("too many polls")
