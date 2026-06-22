#!/usr/bin/env python3
"""EgoVerse R2 access — a tiny stdlib SigV4 signer + bucket lister (zero deps).

Flow: public AWS keys -> AWS Secrets Manager (fetch the R2 creds) -> R2 ListObjectsV2.
Used by download.py and the pipeline to reach the public EgoVerse bucket.
"""
import datetime, hashlib, hmac, json, os, sys, urllib.request, xml.etree.ElementTree as ET

# EgoVerse's read-only keys are PUBLIC (published in their README) but not committed here — GitHub
# blocks AWS keys in repos even when public. Get them from the EgoVerse repo and provide via env:
#   https://github.com/GaTech-RL2/EgoVerse   ("Set up your AWS keys")
#   export EGOVERSE_AWS_KEY=...   EGOVERSE_AWS_SECRET=...
# (or drop a gitignored egoverse_creds.py defining AWS_KEY / AWS_SECRET).
AWS_KEY = os.environ.get("EGOVERSE_AWS_KEY", "")
AWS_SECRET = os.environ.get("EGOVERSE_AWS_SECRET", "")
try:
    from egoverse_creds import AWS_KEY, AWS_SECRET  # optional local override, gitignored
except ImportError:
    pass
SM_REGION = "us-east-2"
R2_SECRET_ID = "r2/rldb/public/credentials"
BUCKET = "rldb"


def _sign(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _signing_key(secret, date, region, service):
    k = _sign(("AWS4" + secret).encode(), date)
    k = _sign(k, region)
    k = _sign(k, service)
    return _sign(k, "aws4_request")


def sigv4(method, url, region, service, key, secret, body=b"", query="", headers=None):
    """Return a urllib Request signed with AWS SigV4."""
    from urllib.parse import urlparse
    u = urlparse(url)
    host = u.netloc
    path = u.path or "/"
    now = datetime.datetime.now(datetime.timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()

    hdrs = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amzdate}
    if headers:
        hdrs.update({k.lower(): v for k, v in headers.items()})
    signed_headers = ";".join(sorted(hdrs))
    canon_headers = "".join(f"{k}:{hdrs[k]}\n" for k in sorted(hdrs))

    canon_req = "\n".join([method, path, query, canon_headers, signed_headers, payload_hash])
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amzdate, scope,
        hashlib.sha256(canon_req.encode()).hexdigest(),
    ])
    sig = hmac.new(_signing_key(secret, datestamp, region, service), to_sign.encode(), hashlib.sha256).hexdigest()
    auth = (f"AWS4-HMAC-SHA256 Credential={key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={sig}")
    req = urllib.request.Request(url + (("?" + query) if query else ""), data=body or None, method=method)
    req.add_header("Authorization", auth)
    for k, v in {**hdrs, "x-amz-date": amzdate}.items():
        if k != "host":
            req.add_header(k, v)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    return req


def get_r2_creds():
    if not (AWS_KEY and AWS_SECRET):
        sys.exit("EgoVerse keys not set. Get the public keys from https://github.com/GaTech-RL2/EgoVerse "
                 "and run: export EGOVERSE_AWS_KEY=... EGOVERSE_AWS_SECRET=...")
    body = json.dumps({"SecretId": R2_SECRET_ID}).encode()
    url = f"https://secretsmanager.{SM_REGION}.amazonaws.com/"
    req = sigv4("POST", url, SM_REGION, "secretsmanager", AWS_KEY, AWS_SECRET, body,
                headers={"content-type": "application/x-amz-json-1.1",
                         "x-amz-target": "secretsmanager.GetSecretValue"})
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(json.loads(r.read())["SecretString"])
    return payload["access_key_id"], payload["secret_access_key"], payload["endpoint_url"]


def list_bucket(key, secret, endpoint):
    """Paginate ListObjectsV2. Returns (count, total_bytes, top-level prefix sizes)."""
    ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
    count, total, prefixes, token = 0, 0, {}, None
    while True:
        q = "list-type=2&max-keys=1000"
        if token:
            from urllib.parse import quote
            q += "&continuation-token=" + quote(token, safe="")
        # query string must be sorted & encoded for signing
        parts = sorted(q.split("&"))
        query = "&".join(parts)
        req = sigv4("GET", f"{endpoint}/{BUCKET}", "auto", "s3", key, secret, query=query)
        with urllib.request.urlopen(req, timeout=60) as r:
            root = ET.fromstring(r.read())
        for c in root.findall(f"{ns}Contents"):
            k = c.findtext(f"{ns}Key")
            sz = int(c.findtext(f"{ns}Size") or 0)
            count += 1
            total += sz
            top = k.split("/")[0]
            d = prefixes.setdefault(top, [0, 0])
            d[0] += 1
            d[1] += sz
        if root.findtext(f"{ns}IsTruncated") == "true":
            token = root.findtext(f"{ns}NextContinuationToken")
        else:
            break
    return count, total, prefixes


def human(n):
    for u in "B KB MB GB TB PB".split():
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}EB"


if __name__ == "__main__":
    print("Fetching R2 credentials from Secrets Manager...", file=sys.stderr)
    ak, sk, ep = get_r2_creds()
    print(f"R2 endpoint: {ep}", file=sys.stderr)
    print("Listing bucket (no data downloaded)...", file=sys.stderr)
    count, total, prefixes = list_bucket(ak, sk, ep)
    print(f"\n=== s3://{BUCKET} ===")
    print(f"objects: {count:,}   total: {human(total)}\n")
    print(f"{'top-level prefix':40} {'objects':>10} {'size':>12}")
    for p, (n, sz) in sorted(prefixes.items(), key=lambda x: -x[1][1]):
        print(f"{p:40} {n:>10,} {human(sz):>12}")
