### Title
Untrusted `.gitmodules` submodule URLs bypass Git's `file://` protocol restriction via forced `protocol.file.allow=always` in `git_repository` submodule handling - (File: `tools/build_defs/repo/git_worker.bzl`)

### Summary
`git_repository`'s submodule handling unconditionally re-enables Git's `file://` transport (which upstream Git disables by default as a security hardening measure) when updating submodules, letting a hostile upstream repository read arbitrary local paths on the build machine into the workspace via a crafted `.gitmodules`.

### Finding Description
When a user sets `init_submodules` or `recursive_init_submodules` on a `git_repository` rule, Bazel runs `git submodule update --init [--recursive] --checkout --force` with the flag `-c protocol.file.allow=always` forced on every invocation: [1](#0-0) 

Upstream Git disabled the `file://` transport for submodules by default (protocol.file.allow=user/deny) specifically because a repository's `.gitmodules` is attacker-controlled content: anyone who can push to (or is the author of) the remote repository being cloned can add a submodule entry such as `submodule.x.url = file:///some/local/path`. Bazel's helper overrides that protection for *every* submodule update, regardless of whether the top-level `remote` is trusted, because `_GitRepoInfo` and `update_submodules` never inspect or restrict the URLs contained in `.gitmodules` — the content comes entirely from the fetched repository, i.e., from the untrusted external server: [2](#0-1) 

Because `commit`/`tag`/`branch` only pin the top-level repository content at fetch time (and the malicious `.gitmodules` can already be present in that very commit/tag/branch the victim depends on, or introduced any time the dependency tracks a branch), an attacker who merely publishes content that a victim's `bazel fetch`/`bazel build` consumes — no access to the victim's machine, credentials, or trusted BUILD/.bzl files — can cause Bazel to `git clone` an arbitrary local filesystem path (e.g., another external repository's cached `.git` checkout in the output base, or any other locally reachable git repository) into the submodule's directory inside the sandboxed external repo tree.

### Impact Explanation
This breaks the containment invariant that content fetched from an external, attacker-served source should not be able to read data from outside its own repository directory. A successful `file://` submodule clone can pull the entire history/content of a local git repository (potentially another private dependency, credentials embedded in commit history, or previously-cloned private mirrors residing in Bazel's output base) into the malicious repository's tree, where it becomes accessible to subsequent build steps, BUILD file glob/exports, or genrules that package repository contents into outputs — i.e., local file/credential exfiltration triggered purely by fetching an untrusted git dependency.

### Likelihood Explanation
Exploitation requires only that a project use `git_repository` with `init_submodules`/`recursive_init_submodules = True` against a dependency the attacker can publish or influence (a common pattern for third-party Git dependencies, forks, or unpinned branches/tags). No interaction with the victim beyond running `bazel fetch`/`bazel build` is needed, and the malicious payload is a single `.gitmodules` entry, making this a low-effort, high-confidence attack vector once the preconditions (submodule init enabled) are met.

### Recommendation
Do not unconditionally force `protocol.file.allow=always` for submodule updates. Restrict allowed submodule protocols to the same protocol as the top-level remote (or to `https`/`ssh`), and only permit `file://` submodule URLs when they resolve to a path known to be inside the same fetch/build sandbox (e.g., validate the resolved path is a descendant of `git_repo.directory` or the repository cache), rejecting/erroring otherwise.

### Proof of Concept
1. Attacker publishes/controls a Git repository `evil.git` containing a `.gitmodules`:
   ```
   [submodule "leak"]
     path = leak
     url = file:///home/victim/.cache/bazel/_bazel_victim/<hash>/external/+git_repository+private_dep/.git
   ```
2. Victim's `MODULE.bazel`/`WORKSPACE` declares:
   ```
   git_repository(
       name = "evil",
       remote = "https://example.com/evil.git",
       commit = "<pinned commit containing the malicious .gitmodules>",
       recursive_init_submodules = True,
   )
   ```
3. `bazel fetch @evil` runs `git -c protocol.file.allow=always submodule update --init --recursive --checkout --force` per `update_submodules` in `tools/build_defs/repo/git_worker.bzl` (lines 210-217), cloning the local `private_dep` checkout's contents into `@evil//leak`, which is then readable/exportable by any target depending on `@evil//leak`. [1](#0-0) 

This should be reproduced as a `src/test/shell/bazel/starlark_git_repository_test.sh` test that sets up a local "victim" git repo, a local "attacker" repo whose `.gitmodules` points at the victim repo's `.git` path via `file://`, enables `recursive_init_submodules`, and asserts the victim repo's private content ends up materialized under the attacker repo's external directory.

### Citations

**File:** tools/build_defs/repo/git_worker.bzl (L41-41)
```text
def git_repo(ctx, directory):
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
