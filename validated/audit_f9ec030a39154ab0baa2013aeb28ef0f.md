### Title
Credential exfiltration via `.netrc` "default" machine fallback in `use_netrc` - (File: tools/build_defs/repo/utils.bzl)

### Summary
The Starlark helper `use_netrc`, used by `http_archive`/`http_file`/`http_jar` (and any repo rule calling `get_auth`) to build the `auth` dict for `repository_ctx.download`, falls back to the `.netrc` "default" machine entry for **any** host that has no explicit `machine` stanza. This breaks the intended invariant that credentials in `.netrc` are host-scoped, and is analogous to the "intended but missing" security control in the report: `.netrc`/HTTP auth is documented and designed to be scoped per-host, but the fallback path silently attaches the user's default credentials to an arbitrary URL whose host is controlled by whoever authored the fetch URL.

### Finding Description
`parse_netrc` stores a `.netrc` "default" stanza under the empty-string key `""` [1](#0-0) .

`use_netrc` then computes, for each URL to be downloaded, the host from the URL and looks up matching credentials. If the host is not explicitly present in the parsed `.netrc`, but a `default` entry (`""`) exists, that default entry's login/password (or bearer token pattern) is used for the URL's host regardless of what that host is: [2](#0-1) 

This function is the sole mechanism (`get_auth` → `use_netrc`) used by the built-in `http_archive`/`http_file`/`http_jar` repository rules to compute the `auth` argument passed to `repository_ctx.download` when a user has any of: an explicit `netrc` attribute, a `NETRC` env var, or a `~/.netrc` file containing a `default` entry [3](#0-2) .

The URLs fed into this function come from the `urls`/`url` attributes of `http_archive`/`http_file` rules, which in the Bzlmod flow are ultimately populated from registry-provided `source.json` mirror URL lists (an untrusted, network-fetched artifact whose *content* — not just its hash — determines which hosts credentials get sent to). Nothing in `use_netrc` restricts the "default" fallback to a set of trusted hosts, and nothing in the calling rules warns or opts the user out of this behavior when multiple, unrelated hosts are contacted for the same repository fetch (mirrors, `distdir`, etc.).

### Impact Explanation
A user who keeps a `default` stanza in `~/.netrc` (a common pattern for a catch-all bearer token, e.g., for a private mirror or CI registry) will have that token/password transparently attached to the `Authorization` header (or basic auth) of **every** unmatched host contacted during repository fetching — including a host chosen by an external, less-trusted party (a registry's mirror URL list, a `bazel_dep`'s `http_archive` definition, or any URL supplied through Bzlmod module resolution). This is a credential-exfiltration primitive: secret material intended for one service is sent to a host the user never intended to authenticate to, satisfying the "hostile origin server" pattern where an attacker only needs to get an untrusted URL into the fetch pipeline (e.g., via a registry entry) to have Bazel's own client leak credentials to them.

### Likelihood Explanation
Requires the victim to have a `default` machine entry in the netrc file resolved via `ctx.attr.netrc`, `NETRC` env var, or `~/.netrc` — a real-world and documented pattern (`use_netrc` docs explicitly describe the empty-string convention as intentional design, not oversight). No sha256/integrity or lockfile mechanism exists to prevent this, because the vulnerability is in **auth header selection**, not content integrity — a pinned sha256 for the archive itself does not stop the credential leak, since the leak happens on the HTTP request before/regardless of the downloaded bytes being verified.

### Recommendation
Remove the unconditional "default"-to-any-host fallback in `use_netrc`, or require an explicit opt-in (e.g., only apply `default` credentials to hosts listed in `auth_patterns`/an explicit allowlist attribute), and document clearly that `.netrc` default entries are broad-scope secrets that will be sent to every unmatched host contacted by a repository rule.

### Proof of Concept
A `BuildIntegrationTestCase`/shell-style proof would:
1. Create a `.netrc` file with only a `default` stanza containing `login foo` / `password SECRET`.
2. Point `NETRC` at that file and define an `http_archive` (or Starlark repo rule via `get_auth`) whose `url` points at an attacker-controlled test HTTP server capturing request headers, with no `machine` entry for that host in `.netrc`.
3. Run `bazel fetch` and assert the attacker server received an `Authorization: Basic ...`/`Bearer SECRET` header despite never having a matching `machine` stanza — confirming credentials for an unrelated "default" host leaked to an arbitrary URL.

This can be built directly on the existing `use_netrc`/`read_netrc` unit-test infrastructure already present in the repo (Starlark unit tests exist for these functions), extended to assert that a URL whose host has no explicit `machine` entry still receives the `default` credentials.

### Citations

**File:** tools/build_defs/repo/utils.bzl (L417-426)
```text
                elif token == "default":
                    # defines the default machine; again, store old machine
                    if not currentmachinename == None:
                        netrc[currentmachinename] = currentmachine

                    # We use the empty string for the default machine, as that
                    # can never be a valid hostname ("default" could be, in the
                    # default search domain).
                    currentmachinename = ""
                    currentmachine = {}
```

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

**File:** tools/build_defs/repo/utils.bzl (L519-541)
```text
def get_auth(ctx, urls):
    """Utility function to obtain the correct auth dict for a list of urls from .netrc file.

    Support optional netrc and auth_patterns attributes if available.

    Args:
      ctx: The repository context of the repository rule calling this utility
        function.
      urls: the list of urls to read

    Returns:
      the auth dict which can be passed to repository_ctx.download
    """
    if hasattr(ctx.attr, "netrc") and ctx.attr.netrc:
        netrc = read_netrc(ctx, ctx.attr.netrc)
    elif "NETRC" in ctx.os.environ:
        netrc = read_netrc(ctx, ctx.os.environ["NETRC"])
    else:
        netrc = read_user_netrc(ctx)
    auth_patterns = {}
    if hasattr(ctx.attr, "auth_patterns") and ctx.attr.auth_patterns:
        auth_patterns = ctx.attr.auth_patterns
    return use_netrc(netrc, urls, auth_patterns)
```
