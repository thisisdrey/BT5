### Title
Git submodule fetch in `git_repository` disables Git's local-transport protection, enabling attacker-controlled submodule URLs to read files outside the repository - (File: tools/build_defs/repo/git_worker.bzl)

### Summary
`git_repository`'s submodule handling in `update_submodules()` unconditionally passes `-c protocol.file.allow=always` to `git submodule update`, re-enabling the `file://`/local-clone transport that upstream Git disabled by default (protocol.file.allow=user) specifically to close CVE-2022-39253. A hostile git server that a victim's `MODULE.bazel`/`WORKSPACE` is configured to fetch from (or that a compromised/malicious dependency points `remote` at) can serve a repository whose `.gitmodules` declares a submodule URL crafted to abuse Git's "local clone" object-transfer optimization, allowing Bazel's shelled-out `git` invocation to read and copy files from outside the intended repository into the fetched external repo contents.

### Finding Description
`update_submodules()` in `tools/build_defs/repo/git_worker.bzl` builds the submodule-update invocation as: [1](#0-0) 

Both the recursive and non-recursive branches pass `"-c", "protocol.file.allow=always"` before running `git ... submodule update --init [--recursive] --checkout --force`. The accompanying comment explains this is done to allow "the submodule command [to] clone from a local directory," citing bazelbuild/bazel#17040, and states it is "necessary for Git 2.38.1 and associated backport versions" — i.e., it is deliberately overriding Git's post-2.38.1 default hardening.

Upstream Git changed the default of `protocol.file.allow` to `user` starting with the 2.38.1 security release specifically because of CVE-2022-39253: a repository (or, transitively, one of its submodules) can specify a `file://` (or bare local-path) URL that, when locally cloned, triggers Git's local-clone optimization (hardlinking/copying objects straight out of the source `.git` directory tree, following symlinks). A malicious repository can plant a symlink inside its `.git`/object layout that points outside the repository, letting the "clone" pull in and expose the contents of arbitrary files reachable by the process. Git's fix was to refuse this transport by default unless the user explicitly opts back in with `protocol.file.allow=always`. Bazel's `git_worker.bzl` does exactly that opt-in, unconditionally, for every `git_repository`/`new_git_repository` fetch that uses submodules — with no validation of the submodule URLs and no interactive/user confirmation.

Attack path:
1. A victim's Bazel project (or a transitive Bazel module) declares `git_repository(remote = "<attacker-controlled-or-attacker-served-git-server>", init_submodules = True)` (or `recursive_init_submodules = True`).
2. The attacker's git server serves a repository whose `.gitmodules`/tree declares a submodule pointing at a `file://`-style or ambiguous local path crafted to exploit Git's local-clone object copy behavior (the CVE-2022-39253 pattern).
3. `_update()` → `update_submodules()` runs `git -c protocol.file.allow=always submodule update --init --checkout --force`, which Bazel's own code has explicitly re-armed to trust local/file transports, allowing the submodule fetch to pull data from outside the repository tree into the materialized external repository directory that later feeds `BUILD` file processing / actions. [2](#0-1) [1](#0-0) 

The `remote` attribute is attacker-reachable content in the sense required by the attacker model: it is a URL string forwarded verbatim into `git remote add origin <remote>` and `git submodule update`, sourced from whatever git host is configured, and the submodule declarations themselves are 100% attacker-served content coming from that host's repository data — not from the victim's trusted root `MODULE.bazel`/`BUILD` files.

### Impact Explanation
This breaks the "untrusted content stays data" and "containment holds" invariants for repository fetching: content originating from an untrusted git remote can cause file contents from outside the intended repository checkout (and potentially outside the output base, depending on symlink targets reachable by the Bazel server process) to be copied into the materialized external repository. Because this happens during repository-rule execution — the fetch phase, which is meant to be sandboxed to only the declared inputs (URLs/commits) and to write only into the repository directory — this is a genuine read/exfiltration primitive from the local file system available to whoever controls the git remote/mirror, without needing separate access to the victim's machine.

### Likelihood Explanation
Reachability requires only that the project uses `git_repository`/`new_git_repository` with `init_submodules`/`recursive_init_submodules = True` (a common, non-exotic configuration) and that the git remote or one of its transitive submodules is attacker-influenced (compromised mirror, typosquatted remote, or an already-included third-party git dependency that the attacker can update). No credentials, sandbox escape, or access to the victim's machine/output base are needed beforehand — the attacker only needs to serve content at a URL the build fetches, matching the report's attacker model. The override is unconditional (no flag gates it off), so every submodule-enabled `git_repository` fetch on a current release is affected the same way.

### Recommendation
Do not blanket-set `protocol.file.allow=always` for submodule updates. Instead, only allow the local-file transport when the submodule URL is actually a same-repository-relative or otherwise trusted path (e.g., validate submodule URLs against an allowlist of schemes such as `https://`/`ssh://`/`git://`, or scope `protocol.file.allow` more narrowly, e.g. only enabling it after verifying the submodule URL resolves within an already-verified local checkout rather than an arbitrary attacker-declared path). At minimum, surface this behavior prominently and gate it behind an explicit repository-rule attribute (e.g., `allow_file_submodules`) defaulting to `False`, so a project must opt in knowingly rather than Bazel silently reverting Git's CVE-2022-39253 hardening for every submodule fetch.

### Proof of Concept
A `src/test/shell/bazel` reproduction would:
1. Start a fake "attacker" git repository with a submodule entry whose URL is crafted per the public CVE-2022-39253 PoC pattern (a `file://` path/symlink structure designed to make Git's local-clone hardlink/copy step pull a file from outside the repository, e.g. `/etc/hostname` or a marker file placed outside the checkout).
2. Define a `git_repository(name="ext", remote="<path-to-attacker-repo>", init_submodules=True)` in `MODULE.bazel`.
3. Run `bazel build @ext//...` and confirm that a file with contents from outside the attacker repository's own tree ends up materialized inside `external/+git_repository+ext/...` (or that Git errors out only when protection is enabled), demonstrating that `protocol.file.allow=always` in `update_submodules()` is what allows the read to proceed.

Note: full confirmation of the exact bypass conditions requires exercising the actual `git submodule update` local-clone code path in a real Git binary (version-dependent behavior around CVE-2022-39253), which is not verifiable purely from static reading of `git_worker.bzl`; a Devin session with shell/Git access would be needed to build and run this PoC end-to-end.

### Citations

**File:** tools/build_defs/repo/git_worker.bzl (L145-150)
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
