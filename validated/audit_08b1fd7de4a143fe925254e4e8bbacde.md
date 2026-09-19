### Title
`git_repository`/`new_git_repository` force-enables `protocol.file.allow=always` for submodule init, letting an attacker-controlled `.gitmodules` clone from arbitrary local filesystem paths - (File: tools/build_defs/repo/git_worker.bzl)

### Summary
Git's fix for the CVE-2015-7545 bug class (unrestricted submodule remote-helper/protocol URLs in `.gitmodules`) is the `protocol.*.allow` family of settings, which since Git 2.38 default `file://` submodule clones to `user` (or otherwise restricted) rather than unconditionally allowed. Bazel's `update_submodules` helper in `git_worker.bzl` explicitly re-opens this by passing `-c protocol.file.allow=always` to `git submodule update --init`, unconditionally, for every `git_repository`/`new_git_repository` invocation that sets `init_submodules`/`recursive_init_submodules`.

### Finding Description
`update_submodules` builds the submodule-update command with `protocol.file.allow=always` hard-coded: [1](#0-0) 

This function is invoked from `_update`, which runs after cloning the attacker-influenceable `remote`/`branch`/`commit` and resetting to the checked-out tree, whenever `init_submodules` or `recursive_init_submodules` is set: [2](#0-1) 

Because the fetched tree (including `.gitmodules`) comes entirely from the untrusted `remote`, an attacker who controls the content served at that remote (or an untrusted branch/PR that CI checks out via `branch`/`commit`, per the threat model) fully controls the `.gitmodules` file that `git submodule update --init` parses. Since Bazel always forces `protocol.file.allow=always`, a submodule URL of the form `file:///some/local/path` will be honored by git regardless of the Git version's own default hardening, letting the attacker direct the submodule clone to read from an arbitrary local filesystem path reachable by the build machine (any local git repository, cache directory, or other checkout) and materialize its contents inside the build's own repository/exec-root tree. This subverts the exact class of defense-in-depth Git added after CVE-2015-7545 (unrestricted submodule protocol selection from untrusted `.gitmodules`), narrowed here to the `file` protocol (the `ext` protocol is not force-enabled by Bazel, so remote-helper command injection is not reachable this way).

### Impact Explanation
An attacker who only controls content that a victim's build fetches (a hostile git remote, or an untrusted branch that CI builds with `git_repository`/`new_git_repository` and `init_submodules`/`recursive_init_submodules = True`) can smuggle a `.gitmodules` entry that redirects a submodule to a `file://` URL pointing outside the intended repository. Because Bazel forces `protocol.file.allow=always`, Git's own protocol restriction — which exists specifically to stop untrusted repository content from directing local-protocol operations — is bypassed. This breaks the containment invariant that untrusted repository content should not be able to pull in filesystem-local data by path; it lets attacker-supplied `.gitmodules` content read from local paths on the build host and stage that content into the fetched repository tree.

### Likelihood Explanation
Likelihood is moderate: it requires the victim's `git_repository`/`new_git_repository` usage to enable submodule initialization (`init_submodules`/`recursive_init_submodules`), which is a common, legitimate configuration for consuming third-party C++/native dependencies with nested submodules. Given that configuration, no additional victim action is needed — the malicious `.gitmodules` is processed automatically as part of the normal fetch flow, and the override is unconditional (not gated on git version or any Bazel flag).

### Recommendation
Do not unconditionally force `protocol.file.allow=always`. Instead, scope the relaxation to only what is required to reproduce the specific local-clone optimization case referenced in bazelbuild/bazel#17040 (e.g., only when the submodule URL is verified to resolve within the already-cloned working tree / same repository root), or expose it as an explicit, opt-in attribute so a project must consciously allow local-protocol submodules rather than having Bazel silently disable Git's own protocol-restriction hardening for every fetch.

### Proof of Concept
1. Set up a malicious git repository `evil.git` whose tip commit contains a `.gitmodules` such as:
   ```
   [submodule "leak"]
       path = leak
       url = file:///etc/or/other/local/git-repo
   ```
2. In a `WORKSPACE`/`MODULE.bazel` (as would occur when CI builds an untrusted PR branch), declare:
   ```python
   git_repository(
       name = "dep",
       remote = "https://attacker-controlled-host/evil.git",
       branch = "main",
       init_submodules = True,
   )
   ```
3. Run `bazel fetch @dep` (or a build depending on `@dep`). Observe via `git -C <repo>/leak remote -v` / process arguments that `git submodule update --init` was executed with `-c protocol.file.allow=always`, as forced in `update_submodules`, and that the `file://` submodule URL from the attacker's `.gitmodules` was honored without any additional configuration by the victim, unlike Git's own default behavior on Git ≥ 2.38 without this override.

I was not able to locate an existing `src/test/shell/bazel` test in `starlark_git_repository_test.sh` that specifically exercises malicious `.gitmodules` with `file://` submodule URLs, nor Bazel's release notes/issue tracker justification text for the `protocol.file.allow=always` override beyond the inline comment; this could not be fully verified without executing the build and would benefit from a Devin session to confirm exact reachability/behavior on a current Bazel release. [1](#0-0)

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
