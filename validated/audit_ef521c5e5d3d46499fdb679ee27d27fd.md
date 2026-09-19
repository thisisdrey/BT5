### Title
Credential exfiltration via mis-parsed authority component in `use_netrc` host extraction - (File: tools/build_defs/repo/utils.bzl)

### Summary
`use_netrc()` in `tools/build_defs/repo/utils.bzl` derives the "host" it uses to look up `.netrc` credentials and `auth_patterns` by naively splitting the URL string on `"/"` and `":"`, rather than properly parsing the URI authority component. A URL containing userinfo before the real host (e.g. `https://trusted-host:1234@evil.com/artifact`) is misparsed: the function extracts `trusted-host` as the "host" (treating the userinfo subfields as `host:port`), while the actual network connection Bazel's downloader makes is to `evil.com`. If `.netrc` (or `auth_patterns`) holds credentials for `trusted-host`, this utility attaches those credentials to a request that is actually sent to the attacker-controlled `evil.com`.

### Finding Description
`use_netrc` computes the host like this: [1](#0-0) 

```
host = schemerest[1].split("/")[0].split(":")[0]
```

For a well-formed URL without userinfo, `schemerest[1].split("/")[0]` is `host[:port]`, and splitting on `":"` correctly isolates the host. But per RFC 3986 the authority component may be `userinfo@host:port`. If a URL such as `https://api.example.com:9999@evil.com/payload.tar.gz` is passed in, `schemerest[1].split("/")[0]` yields `api.example.com:9999@evil.com`, and the trailing `.split(":")[0]` extracts `api.example.com` — completely ignoring the `@evil.com` portion which is the real host that the HTTP client will actually connect to. `use_netrc` (and the analogous host-matching in `use_netrc`'s siblings `get_auth()` and the `auth_patterns` matching in `tools/build_defs/repo/utils.bzl:465-489`) will then find and attach the login/password entry configured for `api.example.com` in the calling repository rule's `auth` dict for that URL, which is later passed to `ctx.download()`/`ctx.download_and_extract()`.

This is the direct analog of the CTFd bug: an identity string (there, a username; here, a URL's authority/host) is parsed/normalized inconsistently between the point where it's used to look up a credential and the point that determines where the credential is actually delivered, allowing an attacker who controls the identity string's construction to redirect trust to themselves.

### Impact Explanation
If exploited, `.netrc` credentials or `auth_patterns` secrets configured by the user/CI for a trusted host (`api.example.com`) are transmitted as an `Authorization` header to an attacker-controlled host (`evil.com`) that the attacker fully controls — a direct credential-exfiltration primitive matching the "Credentials" scope in the bug-class map.

### Likelihood Explanation
`use_netrc`/`get_auth` are the standard helper functions used by `http_archive`, `http_file`, `http_jar`, and custom repository rules (`tools/build_defs/repo/http.bzl`, `jvm.bzl`, `git.bzl`) to build the `auth` argument for downloads. However, for this vulnerability to trigger, the *URL string itself* must contain the crafted userinfo-authority. In stock Bazel usage, URLs in `http_archive`/`http_file` are supplied by the root module's own `BUILD`/`MODULE.bazel`/`.bzl` files (trusted, per the threat model) or by BCR/registry `source.json` archive URLs. The attacker-controlled-URL precondition (a hostile mirror/registry supplying such a userinfo-laden URL, or a redirect injecting one) is not clearly demonstrated as reachable purely from unprivileged/external content without also relying on a trusted root repo choosing to consume that URL as-is or an untrusted registry being selected. I could not confirm, within the available index, whether Bazel's actual Java-side HTTP downloader (`HttpDownloader`/`HttpConnector`) even accepts/dereferences userinfo-bearing URLs the way I hypothesize (I was unable to locate and inspect the relevant Java downloader classes such as `HttpConnectorMultiplexer`, `UrlRewriter`, or the code that consumes the `auth` map keyed by URL, since no matches were found in the indexed Java sources for `Authorization`/`getRequestProperty`/`class Downloader`). This is a significant verification gap.

### Recommendation
Given the above, I cannot confirm this rises to a validated, reproducible finding meeting the strict criteria (a concrete attacker-controlled-content path reaching the vulnerable code, with a runnable JUnit/shell-test proof), largely because:
1. The precondition requires an untrusted/attacker-influenced URL string to reach `use_netrc`, which the threat model restricts (root-repo BUILD/.bzl files are trusted) — I found no concrete mechanism (e.g., BCR `source.json` field, redirect-response `Location` header re-used to build the auth dict, etc.) that lets an unprivileged external party inject such a URL into `use_netrc`'s input list.
2. I was unable to locate and inspect the Java-side downloader code that actually applies this `auth` dict to outgoing HTTP requests, so I could not confirm end-to-end that the Authorization header for `api.example.com` is indeed sent while connecting to `evil.com` rather than the request being rejected or the URL failing to parse as valid.

Given the "Index Coverage" limitation, some Java downloader files (that would contain `HttpConnector`, `UrlRewriter`, or similar classes referenced in the report's "Fetch and integrity" class list) may not be present in this index. I recommend starting a full Devin session with complete repository access to inspect `src/main/java/com/google/devtools/build/lib/bazel/repository/downloader/` (specifically the classes handling `Auth`/`Credentials` application per download URL) to confirm or refute whether the host-mismatch in `use_netrc` is exploitable end-to-end, and if so, to fix `use_netrc`'s host extraction to use a proper URI parser (e.g., strip userinfo before extracting the host) so it agrees with the host that Bazel's actual downloader connects to.

### Proof of Concept
Not produced. A reproducible JUnit/shell test proving actual credential delivery to an attacker-controlled host requires access to the Java HTTP downloader internals I could not locate in the indexed codebase; producing a test only against the Starlark `use_netrc` function would show the host-mismatch in isolation but would not by itself prove that Bazel's live downloader sends the Authorization header to `evil.com` rather than `api.example.com`, or that it rejects the userinfo form outright.

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
