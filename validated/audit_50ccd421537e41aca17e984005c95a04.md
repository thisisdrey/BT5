Based on my research, I found a solid analog to the Gotenberg SSRF bug class in Bazel's `git_repository`/`new_git_repository` implementation.

### Title
`git_repository`'s submodule fetching bypasses Bazel's `--downloader_config` host allowlist/blocklist and per-host credential scoping via attacker-controlled `.gitmodules` content - (File: `tools/build_defs/repo/git_worker.bzl`)

### Summary
Just as Gotenberg's SSRF hardening covered only its own Go HTTP client and missed LibreOffice's independent network fetches of URLs embedded in uploaded OOXML content, Bazel's downloader-level network controls (`--downloader_config`'s `allow`/`block`/`rewrite` directives, implemented for the built-in HTTP downloader) only govern `repository_ctx.download`/`download_and_extract`. They do not — and cannot — govern the network requests made by the system `git(1)` binary that `git_repository`/`new_git_repository` shell out to via `ctx.execute`. When `init_submodules`/`recursive_init_submodules` is enabled, `git submodule update --init` reads `.gitmodules` from the cloned repository content and independently fetches whatever URLs are listed there, using git's own network stack and ambient credential resolution (netrc, `credential.helper`, SSH agent), entirely outside Bazel's UrlRewriter/allowlist and outside the per-URL `auth`/netrc scoping that `repository_ctx.download` provides.

### Finding Description
`git_repository` clones a repo whose `remote` is set by the trusted root config, but the actual bytes of the repository — including `.gitmodules` — are attacker-controlled content (e.g., a compromised upstream maintainer commit, a malicious PR merged into a dependency, or a build referencing an untrusted branch/fork). `git_repo()` in `tools/build_defs/repo/git_worker.bzl` calls `update_submodules`, which runs: [1](#0-0) 

with `protocol.file.allow=always` set specifically to permit submodules to be fetched from arbitrary sources. This is invoked from `_update` whenever `init_submodules`/`recursive_init_submodules` is set: [2](#0-1) 

All git subprocess invocations go through `ctx.execute`, e.g.: [3](#0-2) 

Bazel's own downloader network policy (`--downloader_config`, described as "allow, block or rewrite ... followed by a host name") is documented as applying only to "the remote downloader": [4](#0-3) 

and Bazel's own docs for `--repository_disable_download` explicitly acknowledge this gap: disabling `ctx.download{,_and_extract}` does not stop `ctx.execute` from running an arbitrary executable (like `git`) that accesses the Internet: [5](#0-4) 

The invariant that breaks is the same as in the Gotenberg case: a build-wide network egress control (allowlist/blocklist for outbound fetches during repository fetching) is assumed to be binding for "downloading dependency content," but a subprocess invoked to process that content (`git`, analogous to LibreOffice) performs its own independent network fetches driven by attacker-supplied data (`.gitmodules`) embedded in the very content being fetched, completely bypassing the control.

### Impact Explanation
An attacker who can influence the content of a git repository referenced by `git_repository`/`new_git_repository` with `init_submodules = True` (e.g., a compromised transitive dependency, a malicious contribution merged upstream, or a build that fetches from an untrusted branch) can add or modify `.gitmodules` entries to point at attacker infrastructure or internal-only endpoints. Because the submodule fetch runs through the unrestricted system `git` binary rather than Bazel's downloader:
- Any `--downloader_config block`/`allow` directives meant to restrict network egress during the build are silently bypassed.
- Git will present whatever ambient credentials it is configured with (SSH agent keys, `.netrc`, `credential.helper` tokens for corporate git hosts) to the attacker-specified submodule URL, none of which is scoped the way `repository_ctx.download`'s explicit `auth`/netrc handling is scoped per-URL — this can exfiltrate credentials to an attacker-controlled host.
- It can also be used for SSRF against internal-only network services reachable from the build machine/CI runner (e.g., cloud metadata endpoints), since `git`'s HTTP(S) transport has no host restriction from Bazel's perspective.

### Likelihood Explanation
`init_submodules`/`recursive_init_submodules` are ordinary, documented, non-experimental attributes of `git_repository`, widely used for pulling in vendored dependencies with submodules. Any project depending (directly or transitively) on a git repository with submodules enabled is exposed the moment that upstream repository's content — not the pinned commit hash config in the trusted root, but the tree contents at that commit/branch — is influenced by an attacker (compromised maintainer account, malicious merged PR, or unpinned branch reference). No special local access or credential possession is required by the attacker; they only need write influence over content that ends up checked out.

### Recommendation
- Restrict or explicitly gate submodule fetching so it is subject to the same `--downloader_config` allow/block host policy as `repository_ctx.download`, e.g. by resolving and validating submodule URLs against the active `UrlRewriter`/allowlist before invoking `git submodule update`.
- Avoid passing ambient credential helpers/netrc into the submodule-fetching git invocation by default; require submodule remotes to be explicitly allow-listed or to match the primary `remote`'s host, and drop `protocol.file.allow=always`-style broad trust unless explicitly requested.
- Document prominently (beyond the existing `--repository_disable_download` caveat) that `init_submodules`/`recursive_init_submodules` bypass all Bazel-level network egress and credential-scoping controls, and consider an opt-in flag to disable submodule fetching entirely when repository network egress restrictions are configured.

### Proof of Concept
A `src/test/shell/bazel/starlark_git_repository_test.sh`-style integration test:
1. Set up a local git "upstream" repo `dep` and a local git "submodule-target" repo `sub` served over `http://` on a local test HTTP/git server representing an "internal-only" or "attacker" endpoint (analogous to the canary server in the Gotenberg PoC).
2. In `dep`, add a `.gitmodules` entry pointing `path/to/sub` at the canary server's URL, and commit a gitlink for it (attacker-controlled content, simulating a compromised maintainer commit).
3. In the test WORKSPACE, configure `--experimental_downloader_config`/`--downloader_config` with a `block` directive for the canary server's host, and define:
   ```
   git_repository(
       name = "dep",
       remote = "<trusted dep URL>",
       commit = "<pinned commit>",
       init_submodules = True,
   )
   ```
4. Run `bazel fetch @dep` (or a build depending on it) and observe that the canary server receives the submodule clone request from the `git` subprocess despite the `block` directive for its host — demonstrating that `git_repo()`/`update_submodules` in `tools/build_defs/repo/git_worker.bzl` never consults the `UrlRewriter`/`downloader_config` policy that governs `repository_ctx.download`.

### Citations

**File:** tools/build_defs/repo/git_worker.bzl (L136-150)
```text
def _update(ctx, git_repo):
    ctx.delete(git_repo.directory)

    init(ctx, git_repo)
    add_origin(ctx, git_repo, ctx.attr.remote)
    fetch(ctx, git_repo)
    reset(ctx, git_repo)
    clean(ctx, git_repo)

    if git_repo.recursive_init_submodules:
        ctx.report_progress("Updating submodules recursively")
        update_submodules(ctx, git_repo, recursive = True)
    elif git_repo.init_submodules:
        ctx.report_progress("Updating submodules")
        update_submodules(ctx, git_repo)
```

**File:** tools/build_defs/repo/git_worker.bzl (L210-217)
```text
def update_submodules(ctx, git_repo, recursive = False):
    if recursive:
        # "protocol.file.allow=always" allows the submodule command clone from a local directory.
        # It's necessary for Git 2.38.1 and assoicated backport versions.
        # See https://github.com/bazelbuild/bazel/issues/17040
        _git_maybe_shallow(ctx, git_repo, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive", "--checkout", "--force")
    else:
        _git_maybe_shallow(ctx, git_repo, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--checkout", "--force")
```

**File:** tools/build_defs/repo/git_worker.bzl (L225-230)
```text
def _git(ctx, git_repo, command, *args):
    start = [command]
    st = _execute(ctx, git_repo, start + list(args))
    if st.return_code != 0:
        _error(ctx.name, ["git"] + start + list(args), st.stderr)
    return st.stdout
```

**File:** docs/versions/8.3.1/reference/command-line-reference.mdx (L1255-1256)
```text
`--downloader_config=<a path>` default: see description
:   Specify a file to configure the remote downloader with. This file consists of lines, each of which starts with a directive (`allow`, `block` or `rewrite`) followed by either a host name (for `allow` and `block`) or two patterns, one to match against, and one to use as a substitute URL, with back-references starting from `$1`. It is possible for multiple `rewrite` directives for the same URL to be give, and in this case multiple URLs will be returned.
```

**File:** docs/versions/7.6.1/reference/command-line-reference.mdx (L1563-1564)
```text
`--[no]repository_disable_download` default: "false"
:   If set, downloading using ctx.download\{,\_and\_extract\} is not allowed during repository fetching. Note that network access is not completely disabled; ctx.execute could still run an arbitrary executable that accesses the Internet.
```
