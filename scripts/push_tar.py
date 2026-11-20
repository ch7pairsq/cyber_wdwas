#!/usr/bin/env python3
"""直推 docker save tar 到 Harbor registry（绕过 daemon 代理；Bearer token 流程）。

用法: push_tar.py <tar路径> <repo项目/名> <tag> [--dry-run]
鉴权: /root/.docker/config.json 的 auths[<host>].auth (base64 user:pass)
"""
import base64
import json
import ssl
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

HOST = "portus.in.chaitin.net"
REALM = "https://portus.in.chaitin.net/service/token"
SERVICE = "harbor-registry"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


class Auth:
    def __init__(self):
        cfg = json.loads(Path("/root/.docker/config.json").read_text())
        raw = (cfg.get("auths", {}).get(HOST) or {}).get("auth")
        if not raw:
            sys.exit("no auth for " + HOST)
        self.basic = "Basic " + raw
        self.userpass = base64.b64decode(raw).decode()
        self._token = None

    def token(self, scope):
        url = "{}?service={}&scope={}".format(REALM, SERVICE, scope)
        req = urllib.request.Request(url, headers={"Authorization": self.basic})
        with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
            self._token = json.loads(r.read())["token"]
        return self._token

    def headers(self, scope, extra=None):
        h = {"Authorization": "Bearer " + self.token(scope)}
        if extra:
            h.update(extra)
        return h


def request(method, url, headers=None, body=None, expect=(200,)):
    req = urllib.request.Request(url, headers=headers or {}, data=body, method=method)
    try:
        r = urllib.request.urlopen(req, context=CTX, timeout=300)
    except urllib.error.HTTPError as e:
        if e.code not in expect:
            sys.exit("{} {} -> HTTP {} {}\n{}".format(method, url, e.code, dict(e.headers), e.read()[:300]))
        return e.code, dict(e.headers), e
    if r.status not in expect:
        sys.exit("{} {} -> HTTP {} {}".format(method, url, r.status))
    return r.status, dict(r.headers), r


def head_blob(host, repo, digest, auth):
    """手工跟 307：Harbor 会重定向到 Ceph 签名 URL，跨域时必须剥离
    Authorization（urllib 自动跟随会把 Bearer 头带给 RGW，签名冲突报 400）。
    返回 True=存在 False=不存在 None=未知（调方应重传）。"""
    import http.client
    path = "/v2/{}/blobs/{}".format(repo, digest)
    scope = "repository:{}:pull,push".format(repo)
    for hop in range(3):
        conn = _conn()
        hdrs = auth.headers(scope) if hop == 0 else {}
        conn.request("HEAD", path, headers=hdrs)
        resp = conn.getresponse()
        status, loc = resp.status, resp.getheader("Location")
        resp.read()
        conn.close()
        if status in (301, 302, 303, 307, 308):
            if not loc:
                return None
            import urllib.parse
            u = urllib.parse.urlparse(loc)
            if u.netloc and u.netloc != host:
                # 跨域重定向（如 Harbor → Ceph）：直连目标 host，不带 Bearer。
                conn = http.client.HTTPSConnection(u.netloc, context=CTX, timeout=600)
                conn.request("HEAD", u.path + ("?" + u.query if u.query else ""), headers={})
                resp = conn.getresponse()
                status = resp.status
                resp.read()
                conn.close()
                if status == 200:
                    return True
                if status == 404:
                    return False
                return None
            path = u.path + ("?" + u.query if u.query else "")
            continue
        if status == 200:
            return True
        if status == 404:
            return False
        return None
    return None


def put_blob_from_tar(tarpath, inner, digest, size, host, repo, auth):
    scope = "repository:{}:pull,push".format(repo)
    _, hdrs, _ = request("POST", "https://{}/v2/{}/blobs/uploads/".format(host, repo),
                         headers=auth.headers(scope, {"Content-Length": "0"}), expect=(202,))
    loc = hdrs.get("Location") or hdrs.get("location")
    if loc.startswith("/"):
        loc = "https://" + host + loc
    import urllib.parse
    url = urllib.parse.urlparse(loc)
    sep = "&" if url.query else "?"
    put_path = url.path + ("?" + url.query + "&" if url.query else "?") + "digest=" + digest
    with tarfile.open(tarpath) as tf:
        # 流式提取单个成员上传，避免整层解到磁盘
        src = tf.extractfile(inner)
        body = _ChunkReader(src, size)
        conn = _conn()
        conn.request("PUT", put_path,
                     body=body,
                     headers=auth.headers(scope, {"Content-Length": str(size),
                                                  "Content-Type": "application/octet-stream"}))
        resp = conn.getresponse()
        data = resp.read(300)
        if resp.status not in (201,):
            sys.exit("blob PUT {} -> HTTP {} {}".format(digest[:12], resp.status, data))
        resp.close()
        conn.close()


class _ChunkReader:
    """tar 成员文件对象适配 http.client：按需读取固定块。"""
    def __init__(self, fobj, total):
        self.f, self.left = fobj, total

    def read(self, n=-1):
        if self.left <= 0:
            return b""
        want = 1024 * 1024 if n in (-1, None) else min(n, self.left, 1024 * 1024)
        chunk = self.f.read(want)
        self.left -= len(chunk)
        return chunk


def _conn():
    import http.client
    return http.client.HTTPSConnection(HOST, context=CTX, timeout=600)


def main():
    tarpath, repo_full, tag = sys.argv[1], sys.argv[2], sys.argv[3]
    dry = "--dry-run" in sys.argv
    host = HOST
    auth = Auth()
    if dry:
        tok = auth.token("repository:{}:pull,push".format(repo_full))
        code, _, r = request("GET", "https://{}/v2/{}/tags/list".format(host, repo_full),
                             headers={"Authorization": "Bearer " + tok})
        print("dry-run OK: token acquired, tags/list ->", code, r.read()[:200])
        return

    with tarfile.open(tarpath) as tf:
        manifest = json.loads(tf.extractfile("manifest.json").read())
    entry = manifest[0]
    config_path = entry["Config"]
    layers = entry["Layers"]
    repo = repo_full

    items = []  # (path, kind)
    for p in [config_path] + layers:
        st = tf_member_size(tarpath, p)
        digest = "sha256:" + p.rstrip("/").split("/")[-1] if "/sha256/" in p else None
        if digest is None:
            sys.exit("unexpected tar layout: " + p)
        items.append((p, digest, st))

    with tarfile.open(tarpath) as tf:
        cfg_bytes = tf.extractfile(config_path).read()
    config_digest = "sha256:" + __import__("hashlib").sha256(cfg_bytes).hexdigest()
    cfg_size = len(cfg_bytes)

    for p, digest, size in items:
        exists = head_blob(host, repo, digest, auth)
        if exists is True:
            print("exists:", digest[:20], size, "bytes")
        else:
            if exists is None:
                print("HEAD-unknown, force upload:", digest[:20], size, "bytes")
            else:
                print("upload:", digest[:20], size, "bytes")
            put_blob_from_tar(tarpath, p, digest, size, host, repo, auth)

    m = {"schemaVersion": 2,
         "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
         "config": {"mediaType": "application/vnd.docker.container.image.v1+json",
                    "size": cfg_size, "digest": config_digest},
         "layers": [{"mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                     "size": s, "digest": d} for _, d, s in items[1:]]}
    body = json.dumps(m, separators=(",", ":")).encode()
    code, hdrs, _ = request("PUT", "https://{}/v2/{}/manifests/{}".format(host, repo, tag),
                            headers=auth.headers("repository:{}:pull,push".format(repo),
                                                 {"Content-Type": m["mediaType"],
                                                  "Content-Length": str(len(body))}),
                            body=body, expect=(201,))
    print("manifest PUT ->", code)
    print("digest:", hdrs.get("Docker-Content-Digest"))


def tf_member_size(tarpath, inner):
    import tarfile
    with tarfile.open(tarpath) as tf:
        return tf.getmember(inner).size


if __name__ == "__main__":
    main()
