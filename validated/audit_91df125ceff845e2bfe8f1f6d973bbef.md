Based on what I found, the closest reachable analog in this codebase involves Bazel's netrc-based credential attachment for repository fetches, but I was **not able to fully verify** the Java-side wiring that combines Bzlmod registry `mirror_urls`/`bazel_registry.json` mirrors into the same URL list that gets authenticated (classes like `IndexRegistry`, `RegistryFunction`, `ArchiveRepoSpecBuilder` were not present in the indexed content, likely due to index size limits on this repo, which is documentation-heavy). I can only confirm the Starlark-level credential logic and the documented registry-mirror behavior; a background Devin session with full repo access would be needed to trace the exact Java call path end-to-end.

### Title
Unscoped default-`.netrc` credential leaks to any host in a multi-URL fetch, including untrusted registry mirrors - (File: tools/build_defs/repo/utils.bzl)

### Summary
`get_auth()`/`use_netrc()` in `tools/build_defs/repo/utils.bzl`, used by `http_archive`/`http_file`/`http_jar`, automatically reads the user's `~/.netrc` for every fetch and treats the netrc "default" (empty-machine) entry as matching *any* URL host that isn't explicitly listed, with no host allow-list. If one of the multiple URLs tried for a single repository fetch (e.g. a mirror URL supplied by a registry's `bazel_registry.json` `mirrors` list or a module's `source.json` `mirror_urls`) points at an attacker-controlled host, the ambient default credential is attached to the request to that host.

### Finding Description
`get_auth(ctx, urls)` [1](#0-0)  is invoked by the `http_archive`/`http_file` rules for essentially every fetch. When no explicit `netrc` attribute is set and no `NETRC` env var is present, it falls back to `read_user_netrc(ctx)`, which silently reads the user's own `~/.netrc` file [2](#0-1) .

`use_netrc(netrc, urls, patterns)` then iterates the full list of URLs to fetch and, for each URL, looks up credentials by exact hostname; if the host isn't found, it falls back to the netrc's default entry (`netrc[""]`) and attaches that same login/password (or bearer pattern) to the URL: [3](#0-2) [4](#0-3) 

This mirrors the reported bug class: a caller-unspecified "recipient" (here, a specific authenticated host) silently falls back to an ambient/default identity that was never bound to the URL in question — analogous to `tx.origin` (bootloader) unintentionally receiving the refund when the initiator wasn't the expected kind of account.

Per Bazel/Bzlmod documentation, a registry's `bazel_registry.json` can declare a `mirrors` list, and each module's `source.json` can declare `mirror_urls`; Bazel concatenates these into the URL list tried for the module's `http_archive` fetch [5](#0-4) , with the original source URL as final fallback. These mirror hosts originate from the registry response itself — a "hostile origin server, mirror or registry" is explicitly in scope per the rules, since the registry, not the trusted root config, supplies these hostnames.

The test suite confirms `use_netrc`'s default-entry semantics are real, tested behavior: an entry keyed on the empty-string default machine is applied broadly to any URL not otherwise matched [6](#0-5) , and `http_archive`'s automatic/implicit `.netrc` pickup is exercised without any explicit per-rule `netrc` attribute [7](#0-6) .

### Impact Explanation
If a developer maintains a `~/.netrc` "default" entry (a common pattern for a catch-all bearer token, e.g. for private artifact stores) and resolves a module version from a registry that provides one or more mirror URLs, an attacker who controls (or has compromised) that mirror host will receive an `Authorization` header carrying the user's default credential — even though the credential was never intended for that host. This is credential exfiltration to an attacker-controlled host, matching the "Credentials" bug class (`Netrc`/`NetrcCredentials`) called out in scope.

### Likelihood Explanation
Requires: (1) the victim has a default/catch-all entry in `~/.netrc` (a documented, supported feature, not exotic), and (2) the victim resolves a module from a registry (potentially a third-party/mirrored BCR or any registry added via `--registry`) that supplies a mirror URL under attacker control. Since registries are explicitly untrusted-origin-server surfaces per the rules, and no host-scoping check exists in `use_netrc` today, likelihood is moderate for organizations using default netrc entries alongside third-party/mirrored registries — but it is contingent on the user's `.netrc` configuration, which somewhat limits universality.

### Recommendation
In `use_netrc()`/`get_auth()` (`tools/build_defs/repo/utils.bzl`), stop applying the netrc default/empty-machine entry indiscriminately to every URL host; require the default entry to be opt-in per host (e.g., via an explicit allow-list attribute) or restrict the default entry to only the primary/root-declared URL rather than derived mirror URLs sourced from registries.

### Proof of Concept
Not independently verified end-to-end in this session: I confirmed the vulnerable default-matching logic in `use_netrc` and the documented mirror-URL concatenation behavior, but could not locate/verify the exact Java class(es) (`IndexRegistry`, `RegistryFunction`, `ArchiveRepoSpecBuilder`, or equivalent) that pass registry-declared mirror URLs into the same `urls` list handed to `get_auth`/`http_archive`'s download step, due to index coverage limits on this repository (mostly documentation content was indexed for Java sources). A reproducible `src/test/shell/bazel` proof — standing up a fake registry with a malicious `mirrors` entry, a victim `~/.netrc` default entry, and asserting the credential is sent to the fake mirror host — would need to be constructed with full repository access (e.g., a Devin session) to confirm this crosses from the Starlark utility into the Bzlmod registry fetch path.

### Citations

**File:** tools/build_defs/repo/utils.bzl (L457-463)
```text
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

**File:** tools/build_defs/repo/utils.bzl (L497-517)
```text
def read_user_netrc(ctx):
    """Read user's default netrc file.

    Args:
      ctx: The repository context of the repository rule calling this utility function.

    Returns:
      dict mapping a machine names to a dict with the information provided about them.
    """
    if ctx.os.name.startswith("windows"):
        home_dir = ctx.os.environ.get("USERPROFILE", "")
    else:
        home_dir = ctx.os.environ.get("HOME", "")

    if not home_dir:
        return {}

    netrcfile = "{}/.netrc".format(home_dir)
    if not ctx.path(netrcfile).exists:
        return {}
    return read_netrc(ctx, netrcfile)
```

**File:** tools/build_defs/repo/utils.bzl (L519-537)
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
```

**File:** docs/external/registry.mdx (L45-54)
```text
*   `mirrors`: an array of strings, specifying the list of mirrors to use for
    source archives.
    *   The mirrored URL is a concatenation of the mirror itself, and the
        source URL of the module specified by its `source.json` file sans the
        protocol. For example, if a module's source URL is
        `https://foo.com/bar/baz`, and `mirrors` contains
        `["https://mirror1.com/", "https://example.com/mirror2/"]`, then the
        URLs Bazel will try in order are `https://mirror1.com/foo.com/bar/baz`,
        `https://example.com/mirror2/foo.com/bar/baz`, and finally the original
        source URL itself `https://foo.com/bar/baz`.
```

**File:** src/test/shell/bazel/starlark_repository_test.sh (L1619-1651)
```shellscript
function test_use_netrc() {
    # Test the starlark utility function use_netrc.
  cat > .netrc <<'EOF'
machine foo.example.org
login foousername
password foopass

machine bar.example.org
login barusername
password passbar🌱

# following lines mix tabs and spaces
machine	  oauthlife.com
	password	TOKEN

# Password-only auth credentials, will not be passed into `patterns` like oauthlife.com.
machine baz.example.org password ABCDEFG

# Test for warning mechanism.
machine qux.example.org
EOF
  # Read a given .netrc file and combine it with a list of URL,
  # and write the obtained authentication dictionary to disk; this
  # is not the intended way of using, but makes testing easy.
  cat > def.bzl <<'EOF'
load("@bazel_tools//tools/build_defs/repo:utils.bzl", "read_netrc", "use_netrc")
def _impl(ctx):
  print("authrepo is being evaluated")
  rc = read_netrc(ctx, ctx.attr.path)
  auth = use_netrc(rc, ctx.attr.urls, {"oauthlife.com": "Bearer <password>",})
  ctx.file("data.bzl", "auth = %s" % (auth,))
  ctx.file("BUILD", "")
  ctx.file("WORKSPACE", "")
```

**File:** src/test/shell/bazel/starlark_repository_test.sh (L1926-1950)
```shellscript
function test_http_archive_implicit_netrc() {
  mkdir x
  echo 'exports_files(["file.txt"])' > x/BUILD
  echo 'Hello World' > x/file.txt
  tar cvf x.tar x
  sha256=$(sha256sum x.tar | head -c 64)
  serve_file_auth x.tar

  export HOME=`pwd`
  if is_windows; then
    export USERPROFILE="$(cygpath -m ${HOME})"
  fi
  cat > .netrc <<'EOF'
machine 127.0.0.1
login foo
password bar
EOF

  mkdir main
  cd main
  cat > $(setup_module_dot_bazel) <<EOF
http_archive = use_repo_rule("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")
http_archive(
  name="ext",
  url = "http://127.0.0.1:$nc_port/x.tar",
```
