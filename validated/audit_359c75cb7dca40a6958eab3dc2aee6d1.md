### Title
Bazel's `git_repository`/`new_git_repository` submodule fetching disables Git's `protocol.file.allow` hardening, letting an attacker-controlled `.gitmodules` read arbitrary local paths - (File: `tools/build_defs/repo/git_worker.bzl`)

### Summary
`git_repository` and `new_git_repository` support `init_submodules`/`recursive_init_submodules`, which run `git submodule update`. Bazel explicitly forces `-c protocol.file.allow=always` on that invocation, which disables the `file://`-submodule restriction that upstream Git ships by default specifically to prevent malicious repositories from using `.gitmodules` to make Git read/clone arbitrary local paths on the machine performing the checkout.

### Finding Description
`update_submodules` in `tools/build_defs/repo/git_worker.bzl` runs: [1](#0-0) 

Both the recursive and non-recursive branches pass `-c protocol.file.allow=always` before invoking `git submodule update --init ... --checkout --force`. This is executed through `_git_maybe_shallow` → `_execute`, which runs the plain `git` binary (not through a shell, so classic shell metacharacter injection is not the vector) but does pass this config flag verbatim into Git's own config parsing: [2](#0-1) 

Git added `protocol.file.allow` (defaulting to `user`, i.e. disallowed for submodule-driven, non-interactive operations) as a hardening measure after the well-known submodule-URL attack class (e.g. the `ext::`/`file::` submodule RCE and local file disclosure issues fixed around Git 2.7.1/2.17). The intent of that default is: submodule URLs come from `.gitmodules`, which is untrusted, attacker-influenced content shipped inside the repository being cloned - not something the invoking user typed - so Git refuses to let such an untrusted URL point at the local filesystem. By passing `protocol.file.allow=always`, Bazel unconditionally overrides this protection for every submodule fetch it performs, regardless of where the top-level repository came from.

The `remote`, `commit`/`tag`/`branch` attributes are pinned/typed by the trusted root module, but the **content of `.gitmodules`** inside the fetched tree is attacker-controlled in the ordinary case where `git_repository`/`new_git_repository` is configured with `tag=`/`branch=` (a moving ref, not a fixed content-addressed `commit=`), or where the initial commit is being resolved for the first time. In these very common configurations there is no sha256/content digest recorded anywhere by Bazel to bind the fetched tree to previously-reviewed content — the mutable ref itself is the only pointer, and it is served by whatever mirror/host is designated in `remote`. Any party who can push to that branch/tag (a compromised or malicious upstream maintainer, a hijacked mirror, or an untrusted CI branch that gets built) can add a `.gitmodules` entry such as:
```
[submodule "x"]
    path = x
    url = file:///etc/some-sensitive-path
```
Because Bazel forces `protocol.file.allow=always`, Git will clone that local path directly into the external repository's checkout tree instead of refusing it, copying local filesystem contents that were never part of the intended remote repository into the Bazel workspace, where they become inputs to the build (e.g. via glob patterns, `strip_prefix`, or being packaged into build outputs/artifacts that the attacker-influenced build later exposes).

### Impact Explanation
This breaks the invariant that "untrusted content stays data" and that the checkout is confined to what the declared remote actually serves: a `.gitmodules` file supplied by an attacker-controlled/mutable upstream ref causes Bazel's Git invocation to read files from the local filesystem of the machine running the build (paths outside of the intended repository/exec root), effectively an attacker-directed local file read/exfiltration primitive whenever `init_submodules`/`recursive_init_submodules` is enabled.

### Likelihood Explanation
`init_submodules`/`recursive_init_submodules` are documented, commonly-enabled attributes of `git_repository`/`new_git_repository`. Any external dependency fetched via `tag=`/`branch=` (rather than a pinned `commit=`) is fetched from a mutable ref with no digest verification, so the maintainer of that upstream repo, or anyone able to push to the referenced branch/tag (e.g., an untrusted CI branch scenario), can add the malicious `.gitmodules` entry without any special access to the victim's build. The `protocol.file.allow=always` override was added deliberately (per the code comment, for local-clone submodule use cases, referencing bazelbuild/bazel#17040) and applies unconditionally, with no scoping to same-origin or trusted paths.

### Recommendation
Do not blanket-override `protocol.file.allow` to `always`. At minimum, restrict it to `protocol.file.allow=user` (or leave Git's default) and only relax it for the specific, already-trusted local-clone scenarios that motivated the change (e.g., detect when the submodule URL is verified to be inside the already-fetched, integrity-checked repository, or make the override opt-in via an explicit attribute rather than always-on).

### Proof of Concept
1. Create an upstream Git repository referenced by a `git_repository(... branch="main", init_submodules=True)` (or `recursive_init_submodules=True`) declaration.
2. As the attacker-controlled maintainer of that branch, commit a `.gitmodules` file containing a submodule with `url = file:///path/to/sensitive/local/directory` and push it to `main`.
3. Run a Bazel build that fetches this `git_repository`. Observe that `git -c protocol.file.allow=always submodule update --init ...` (see `update_submodules`, `tools/build_defs/repo/git_worker.bzl` lines 210-217) succeeds in cloning `/path/to/sensitive/local/directory` into the external repository's checkout tree, whereas running the equivalent `git submodule update --init` without the forced override would fail with `fatal: transport 'file' not allowed`.
4. A `src/test/shell/bazel/starlark_git_repository_test.sh`-style integration test can assert this by: (a) setting up a local bare "upstream" repo with such a `.gitmodules` pointing to a sentinel file/directory outside the repo, (b) running Bazel's `git_repository` rule against it with `init_submodules = True`, and (c) verifying the sentinel content is now present inside the external repository's checkout in the output base — demonstrating a read that escapes the intended remote content boundary. [3](#0-2)

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

**File:** tools/build_defs/repo/git_worker.bzl (L311-319)
```text
def _execute(ctx, git_repo, args):
    # "core.fsmonitor=false" disables git from spawning a file system monitor which can cause hangs when cloning a lot.
    # See https://github.com/bazelbuild/bazel/issues/21438
    start = ["git", "-c", "core.fsmonitor=false"]
    return ctx.execute(
        start + args,
        environment = ctx.os.environ | _GIT_LOCAL_ENV_VARS,
        working_directory = str(git_repo.directory),
    )
```

**File:** src/test/shell/bazel/starlark_git_repository_test.sh (L1-1)
```shellscript
#!/usr/bin/env bash
```
