### Title
Bazel forcibly disables Git's `protocol.file.allow` submodule hardening, enabling arbitrary local file read/write via malicious submodule URLs during `git_repository(recursive_init_submodules = True)` - ([File: tools/build_defs/repo/git_worker.bzl])

### Summary
`git_repository`/`new_git_repository` with `init_submodules`/`recursive_init_submodules = True` runs `git submodule update --init [--recursive] --checkout --force` with the config override `-c protocol.file.allow=always` unconditionally set, in `update_submodules` in `tools/build_defs/repo/git_worker.bzl`. [1](#0-0) 

### Finding Description
Bazel pins the top-level Git repository content by `commit` (or `tag`/`branch`), giving the appearance of integrity control, but `.gitmodules` inside that fetched tree is completely attacker-controlled content served from the remote the build fetches from. When `init_submodules`/`recursive_init_submodules` is set, Bazel calls `git submodule update --init [--recursive] --checkout --force` and explicitly forces `-c protocol.file.allow=always`: [1](#0-0) 

Git's own default posture (`protocol.file.allow=user`) restricts `file://` submodule URLs specifically because upstream Git has repeatedly needed to harden `file://`/local-clone handling against submodule-triggered local clones that can escape the intended checkout via hardlinks/symlinks and local-clone optimizations (e.g., the class of bugs fixed by Git's CVE-2022-39253 and related advisories). By overriding this to `always`, Bazel removes that guard for every `git_repository` invocation with submodule init enabled, regardless of how untrusted the fetched repository's `.gitmodules` file is.

An attacker who merely controls the content of a Git repository that a victim's build fetches (e.g., a repo referenced by `remote`/`commit` in `MODULE.bazel`, or a third-party dependency's own submodules fetched transitively) can add a `.gitmodules` entry pointing a submodule at a crafted `file://` path (potentially combined with symlinks in their own tree) that abuses Git's local-clone path to read or link files from outside the intended checkout directory into the build's external repository tree. The commit-pinning integrity that `git_repository` provides only binds the top-level tree hash — it does nothing to vet what URLs an included `.gitmodules` points at, nor does it stop Bazel from re-enabling a protocol class Git deliberately restricts by default for this exact reason.

### Impact Explanation
This breaks the "containment holds" invariant for repository fetches: content chosen entirely by the untrusted repository owner (a `.gitmodules` file) can, via Git's own local-clone/hardlink handling under `protocol.file.allow=always`, cause the submodule clone step to read or place files outside the boundary the user expects Bazel to keep information from (i.e., outside of the external repository directory Bazel manages under the output base). Depending on the exact Git version's local-clone/hardlink behavior this can manifest as disclosure of arbitrary local files into the build tree or corruption/replacement of files via hardlinked/symlinked local clone artifacts, which then get built into the user's targets.

### Likelihood Explanation
This requires only `recursive_init_submodules = True` (or `init_submodules = True`) on a `git_repository` — a document, common flag for projects that use submodules, combined with the victim building/fetching a rule that pulls in a repository (possibly transitively via a module extension) containing a hostile `.gitmodules`. No credentials, output-base access, or root-repo BUILD/.bzl trust is required from the attacker; only the ability to publish/host a Git repository (or control what a `.gitmodules` entry the build ends up fetching contains). The attacker is fully unprivileged relative to the victim's machine.

### Recommendation
Do not force `protocol.file.allow=always` for submodule updates. Either drop the override entirely (rely on Git's safer default) or scope it narrowly — e.g., only allow `file://` for the specific local path Bazel itself controls, rather than granting blanket permission for arbitrary submodule URLs discovered inside untrusted repository content. If the override exists to support a specific legitimate local-clone use case (e.g., `bazel--001`'s comment about Git 2.38.1 compat, referencing bazelbuild/bazel#17040), it should be conditioned on verifying the submodule URLs are within the expected repository/remote, not applied unconditionally to `git submodule update --init [--recursive]`.

### Proof of Concept
A `src/test/shell/bazel` proof would:
1. Create a "victim" local filesystem path with a marker file outside any Bazel-managed directory (e.g., `$TEST_TMPDIR/secret/marker`).
2. Create a malicious upstream Git repo with a `.gitmodules` entry using a `file://` URL and a crafted internal tree (following the pattern of Git's local-clone/hardlink submodule advisories) designed to place/link `$TEST_TMPDIR/secret/marker` (or another out-of-tree file) into the submodule checkout path.
3. Reference this malicious repo via `git_repository(..., recursive_init_submodules = True)` in `MODULE.bazel`, pinned by commit hash (simulating a fully "integrity-checked" top-level fetch).
4. Run `bazel fetch` / `bazel build` and assert that the resulting submodule checkout under the external repository directory contains content originating from outside the intended source tree (i.e., contains `marker`'s content), demonstrating the containment bypass enabled by the forced `protocol.file.allow=always` config in `update_submodules`. [2](#0-1) 

Note: I could not verify the exact installed Git version behavior or confirm a currently-exploitable hardlink/symlink escape sequence against the latest Git release from within this codebase alone (that requires testing against a live `git` binary), so the concrete file-escape payload in step 2 would need to be validated against the Git version(s) Bazel CI/test infra uses.

### Citations

**File:** tools/build_defs/repo/git_worker.bzl (L145-151)
```text
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
