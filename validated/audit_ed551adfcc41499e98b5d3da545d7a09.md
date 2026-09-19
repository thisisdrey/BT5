Based on my research, I found a strong analog to the "whitelist without revocation" bug class in Bazel's Bzlmod lockfile handling of yanked (revoked) module versions.

## Analog Finding

### Title
Lockfile `update` mode does not re-check yanked (revoked) module versions, allowing a previously-accepted malicious registry module to remain trusted indefinitely - (File: `MODULE.bazel.lock` / lockfile resolution logic, documented in `docs/external/lockfile.mdx`)

### Summary
Just as the Arborist in Merkle.sol could be whitelisted but never removed from the trust set even after misbehaving, a Bazel module version that is accepted during dependency resolution and recorded in the `MODULE.bazel.lock` file is not re-validated against the registry's "yanked versions" list on subsequent builds under the default `--lockfile_mode=update`.

### Finding Description
Bzlmod resolves external dependencies from module registries (e.g., the Bazel Central Registry). When a module version is selected, Bazel records its `registryFileHashes` and, if yanked versions were explicitly allowed, its status in the `selectedYankedVersions` section of the lockfile [1](#0-0) . The docs explicitly state that in the default `update` mode, "Bazel also avoids refreshing mutable information, such as yanked versions, for dependencies that haven't changed" [2](#0-1) . Refreshing this "yanked" status only occurs under `--lockfile_mode=refresh`, and even then only "roughly every hour while in this mode" [3](#0-2) .

This means: an attacker who publishes a malicious module version to a registry the victim's build consumes can get that version selected and locked into `MODULE.bazel.lock` (analogous to the Arborist being "whitelisted"). If the registry maintainers later discover the module is malicious and yank (revoke) it — the intended revocation mechanism — builds using the default `update` lockfile mode will continue to silently reuse the already-locked, now-revoked version without re-checking the yank status, because that mutable trust decision, once recorded, has no automatic path to being un-recorded/revoked on ordinary builds.

### Impact Explanation
This breaks the intended security invariant of the yanked-versions mechanism: that a registry can revoke trust in a compromised module version and have consumers stop using it. Instead, once a module version is accepted and locked, ordinary `bazel build`/`bazel test` invocations (which default to `--lockfile_mode=update`) keep trusting it indefinitely unless a developer explicitly runs `bazel mod deps --lockfile_mode=refresh`. This is directly analogous to the Merkle.sol Arborist bug: revocation of a previously-granted trust decision is not enforced by default.

### Likelihood Explanation
This requires no privileged access — only that a project depend on a registry module version an attacker can influence or publish (e.g., an open registry, a compromised maintainer account, or a module the attacker controls that later gets flagged/yanked). Since `update` is the default lockfile mode used by virtually all CI and local builds, the revocation gap is hit by default, not an edge case.

### Recommendation
Consider tightening `update` mode so that yanked-version status (and other network-verifiable, security-relevant mutable registry state) is re-checked periodically or on every build unless explicitly pinned via `--lockfile_mode=off`/frozen mode, rather than only under `refresh`. Alternatively, surface a stronger warning/error when a locked module version is discovered to have been yanked, even while operating in `update` mode.

### Proof of Concept
A reproducible scenario following the pattern of `src/test/py/bazel/bzlmod/bazel_lockfile_test.py` [4](#0-3) :
1. Set up a local static-file `BazelRegistry` (as in `src/test/py/bazel/bzlmod/bzlmod_credentials_test.py`) serving module `evil@1.0`.
2. Build with `bazel_dep(name="evil", version="1.0")`; this locks the module's registry file hashes into `MODULE.bazel.lock`.
3. Update the registry's metadata to mark `evil@1.0` as yanked (simulating revocation after compromise discovery).
4. Re-run `bazel build` with default (`update`) lockfile mode and observe the build succeeds without error or re-fetch, still trusting `evil@1.0`, per the documented behavior that mutable info like yanked versions isn't refreshed for unchanged dependencies in `update` mode [5](#0-4) .
5. Only `bazel mod deps --lockfile_mode=refresh` detects and errors on the now-yanked version.

**Caveat**: This analysis is based on Bazel's documented lockfile semantics; the Java implementation classes (`YankedVersionsUtil`, `BazelLockFileFunction`) were not present in the indexed portion of this codebase, so the precise code path enforcing/omitting this check could not be directly cited from source. Due to index size limits, some file contents may not be available — starting a full Devin session would allow inspection of the actual `YankedVersionsUtil`/`BazelLockFileFunction`/`Selection` Java sources to confirm the exact enforcement point and produce a JUnit-level PoC.

### Citations

**File:** docs/external/lockfile.mdx (L36-43)
```text
lockfile. The available modes are:

*   `update` (Default): Use the information that is present in the lockfile to
    skip downloads of known registry files and to avoid re-evaluating extensions
    whose results are still up-to-date. If information is missing, it will
    be added to the lockfile. In this mode, Bazel also avoids refreshing
    mutable information, such as yanked versions, for dependencies that haven't
    changed.
```

**File:** docs/external/lockfile.mdx (L166-176)
```text
Bazel uses the hashes from the lockfile to look up registry files in the
repository cache before downloading them, which speeds up subsequent
resolutions.

### Selected Yanked Versions {#selected-yanked-versions}

The `selectedYankedVersions` section contains the yanked versions of modules
that were selected by module resolution. Since this usually result in an error
when trying to build, this section is only non-empty when yanked versions are
explicitly allowed via `--allow_yanked_versions` or
`BZLMOD_ALLOW_YANKED_VERSIONS`.
```

**File:** src/test/py/bazel/bzlmod/bazel_lockfile_test.py (L2975-3013)
```python
  def testUnicode(self):
    self.ScratchFile(
        'MODULE.bazel',
        [
            'lockfile_ext = use_extension("extension.bzl", "lockfile_ext")',
            'use_repo(lockfile_ext, "hello")',
        ],
    )
    self.ScratchFile('BUILD.bazel')
    self.ScratchFile(
        'extension.bzl',
        [
            'def impl(ctx):',
            (
                '    ctx.file("BUILD",'
                " \"filegroup(name='lala')\\nprint('Unicode test:"
                ' {}\')".format(ctx.attr.str))'
            ),
            '',
            'repo_rule = repository_rule(',
            '    implementation=impl,',
            '    attrs = {',
            '        "str": attr.string(),',
            '    },',
            ')',
            '',
            'def _module_ext_impl(ctx):',
            '    print("Hello from the other side!")',
            '    repo_rule(name="hello", str="äöüÄÖÜß🌱")',
            '',
            'lockfile_ext = module_extension(',
            '    implementation=_module_ext_impl,',
            ')',
        ],
    )

    _, _, stderr = self.RunBazel(['build', '@hello//:all'])
    self.assertIn('Hello from the other side!', ''.join(stderr))
    self.assertIn('Unicode test: äöüÄÖÜß🌱', ''.join(stderr))
```
