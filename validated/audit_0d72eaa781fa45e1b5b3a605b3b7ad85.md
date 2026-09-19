### Title
Non-reproducible module extension `facts` in `MODULE.bazel.lock` are trusted and merged without integrity verification, allowing lockfile tampering on an untrusted branch to poison future extension evaluations - (File: `src/test/py/bazel/bzlmod/bazel_lockfile_test.py`, feature: Bzlmod module extension `facts`/`facts_version`)

### Summary
Bazel's Bzlmod `facts` mechanism (`extension_metadata(facts=...)`, `module_ctx.facts`, `facts_version`) is explicitly designed so that, for a **non-reproducible** module extension, values written into the `facts` section of the (checked-in) `MODULE.bazel.lock` are trusted "as is" and merged into future evaluations without re-running the extension and without any check against a digest (`bzlTransitiveDigest`/`usagesDigest`) or checksum. The stated rationale, confirmed by the test suite, is that "a rerun could require network access or credentials that are only available to the person updating the pins" [1](#0-0) . Since `facts` are documented as the canonical place to store "the hashes of artifacts in an immutable repository" and "download URL and checksum" pairs that are otherwise unguarded by a real checksum download [2](#0-1) , an attacker who can get a victim's build to consume a modified `MODULE.bazel.lock` (e.g. via a pull-request branch that CI checks out and builds) can inject or alter a `facts` entry for a non-reproducible extension and have it silently accepted and reused by future evaluations of that extension.

### Finding Description
The lockfile format documents `facts` as arbitrary JSON-like data "always persisted in the lockfile and available to future evaluations of the extension via the `facts` field of `module_ctx`" [3](#0-2) . Extension authors are explicitly told to use `facts` to cache "effectively immutable data obtained from outside the build... but you don't have a checksum available to guard the download," with the canonical example being "a mapping from version numbers of some SDK to the an object containing the download URL and checksum of that version" [2](#0-1) .

Bazel's own test suite documents the trust model directly: for a **non-reproducible** extension, if the workspace lockfile's `facts` for that extension change "without any of its inputs changing (e.g. a branch switch or pull brings in a re-pin performed on another machine), the cached result must be reused with the new facts as is" [1](#0-0) . The test `testExternallyUpdatedFactsKeptByUpdateModeForNonReproducibleExtension` demonstrates that injecting an attacker-chosen `facts` entry (`{'1.27.0': {'hash': 'hola'}}`) directly into `MODULE.bazel.lock` and rebuilding with `--lockfile_mode=update` results in the extension **not** being re-evaluated and the injected value being kept verbatim in the lockfile [4](#0-3) . This is in stark contrast to the `reproducible=True` case, where an edited `facts` entry *does* trigger re-evaluation and is discarded/regenerated, because "a reproducible extension's result is only ever cached in the machine-local hidden lockfile" [5](#0-4) . There is no equivalent regression test showing `--lockfile_mode=error` rejecting a tampered `facts` entry for a **non-reproducible** extension (only for a `reproducible=True` one, see `testDeletedFactsDetectedByErrorModeForReproducibleExtension` [6](#0-5) ), indicating the non-reproducible path treats `facts` content as outside the scope of lockfile-integrity verification entirely.

The invariant that breaks: the lockfile is supposed to be the authoritative, verifiable record that "determine[s] whether the project state has changed" via hashes/digests (`registryFileHashes`, `bzlTransitiveDigest`, `usagesDigest`) [7](#0-6) , but the `facts` sub-map — explicitly intended to hold checksums/URLs used in lieu of real download verification — is not itself covered by any digest that gates re-evaluation for non-reproducible extensions. A hostile branch/PR author who edits the checked-in `MODULE.bazel.lock` can therefore plant an attacker-chosen checksum/URL pair that the extension will trust on the next `update`-mode evaluation without re-fetching or re-verifying anything, and this poisoned fact persists in the hidden per-output-base lockfile copy for reuse across subsequent builds on that machine.

### Impact Explanation
If a module extension follows Bazel's documented pattern and uses `facts` to store an unverified checksum/URL for a download, an attacker who controls a branch or PR that a victim's CI builds can smuggle a modified `facts` entry into `MODULE.bazel.lock`. Because Bazel reuses non-reproducible extension facts "as is" without re-running the extension or validating them against `bzlTransitiveDigest`/`usagesDigest`, the extension will use the attacker-supplied checksum/URL pair on the next evaluation, allowing it to fetch and trust attacker-controlled content as if it had been through the normal checksum-verification path documented for the lockfile. This is a lockfile-integrity/checksum bypass reaching a build that consumed the tampered branch, and the poisoned fact is written into the machine-local hidden lockfile, potentially affecting later builds too.

### Likelihood Explanation
This requires: (1) a module extension author following the documented `facts`-for-unverified-downloads pattern, (2) `--lockfile_mode=update` (a supported, arguably default-adjacent workflow for CI/dependency-refresh jobs) being used when building an untrusted branch/PR that contains a modified `MODULE.bazel.lock`. Both conditions are realistic for CI setups that build external PRs and periodically refresh lockfiles. The behavior is intentional/by-design per the code comments and tests rather than a coding accident, but it is a documented trust gap: the mechanism meant to hold "immutable" checksums has no integrity check of its own, unlike the rest of the lockfile.

### Recommendation
- Bind non-reproducible extensions' `facts` to a verifiable digest (e.g., require/allow extension authors to record and check a checksum of the facts contents against a separately-authenticated value), or
- At minimum, make `--lockfile_mode=error` explicitly detect and fail on unexpected `facts` changes for non-reproducible extensions (mirroring the reproducible-extension behavior), so CI pipelines that pin/lock external contributions can reject a tampered lockfile instead of silently trusting it.
- Document loudly that `facts` used to store checksums/URLs are not integrity-protected by Bazel and that extension authors must not rely on lockfile `facts` alone as a substitute for `ctx.download(..., sha256=...)` on branches originating from untrusted contributors.

### Proof of Concept
The existing regression test in the repository already demonstrates the acceptance path (only the "attacker" role needs to be substituted for "another machine's legitimate re-pin"): [8](#0-7)  (`_addFactToLockfile`, which "Simulates the workspace lockfile changing underneath a warm output base, e.g. due to a branch switch or pull") combined with [4](#0-3)  (`testExternallyUpdatedFactsKeptByUpdateModeForNonReproducibleExtension`) shows that a manually-injected `facts` value survives a `bazel build --lockfile_mode=update` unchanged and without the extension being re-evaluated, i.e., without any validation of the injected content.

**Caveat / uncertainty:** I was unable to locate the Java implementation (`SingleExtensionEvalFunction`, `LockFileModuleExtension`, `BazelLockFileFunction`) in the indexed codebase to directly confirm at the source-code level how `facts` are compared/merged versus `bzlTransitiveDigest`/`usagesDigest`, or to confirm the exact default value of `--lockfile_mode`. My analysis relies on the documentation (`docs/external/extension.mdx`, `docs/external/lockfile.mdx`) and the Python integration test suite (`bazel_lockfile_test.py`), which describe and test this behavior explicitly, but a full read of the Java source would be needed to state definitively whether `--lockfile_mode=error` catches this for non-reproducible extensions. Given the size limits on the indexed codebase, some file contents (particularly the relevant `.java` sources under `src/main/java/com/google/devtools/build/lib/bazel/bzlmod/`) were not available to this search; a Devin session with full repository access would be needed to pinpoint and confirm the exact code path.

### Citations

**File:** src/test/py/bazel/bzlmod/bazel_lockfile_test.py (L3647-3656)
```python
  def _addFactToLockfile(self, extension_id, fact_key, fact_value):
    """Adds a facts entry to the workspace lockfile."""
    # Simulates the workspace lockfile changing underneath a warm output base,
    # e.g. due to a branch switch or pull that brings in facts produced by an
    # evaluation of the extension on a different machine.
    with open(self.Path('MODULE.bazel.lock'), 'r') as f:
      lockfile = json.loads(f.read().strip())
    lockfile['facts'][extension_id][fact_key] = fact_value
    with open(self.Path('MODULE.bazel.lock'), 'w') as f:
      json.dump(lockfile, f, indent=2)
```

**File:** src/test/py/bazel/bzlmod/bazel_lockfile_test.py (L3725-3737)
```python
  def testExternallyUpdatedFactsKeptByUpdateModeForNonReproducibleExtension(
      self,
  ):
    """UPDATE mode keeps updated facts of a non-reproducible extension.

    The hidden lockfile records the facts produced by the most recent local
    evaluation of the extension. If the workspace lockfile's facts for a
    non-reproducible extension change without any of its inputs changing (e.g.
    a branch switch or pull brings in a re-pin performed on another machine),
    the cached result must be reused with the new facts as is: a cold machine
    would not rerun the extension either and a rerun could require network
    access or credentials that are only available to the person updating the
    pins.
```

**File:** src/test/py/bazel/bzlmod/bazel_lockfile_test.py (L3739-3756)
```python
    extension_id = self._setUpExtensionWithFacts(reproducible=False)
    self._addFactToLockfile(extension_id, '1.27.0', {'hash': 'hola'})

    _, _, stderr = self.RunBazel(
        ['build', '@hello//:all', '--lockfile_mode=update']
    )
    self.assertNotIn('lockfile_ext is being evaluated', '\n'.join(stderr))

    with open(self.Path('MODULE.bazel.lock'), 'r') as f:
      lockfile = json.loads(f.read().strip())
    self.assertEqual(
        lockfile['facts'][extension_id],
        {
            '1.25.0': {'hash': 'olleh'},
            '1.26.1': {'hash': 'hello'},
            '1.27.0': {'hash': 'hola'},
        },
    )
```

**File:** src/test/py/bazel/bzlmod/bazel_lockfile_test.py (L3758-3781)
```python
  def testExternallyUpdatedFactsRegenerateReproducibleExtension(self):
    """UPDATE mode reruns a reproducible extension on changed facts.

    A reproducible extension's facts can only legitimately change together
    with its inputs, so facts that differ from the record of its most recent
    evaluation despite unchanged inputs indicate an edited workspace lockfile
    and the extension is rerun to regenerate them. This is safe: a reproducible
    extension's result is only ever cached in the machine-local hidden
    lockfile, so a cold machine would rerun it anyway.
    """
    extension_id = self._setUpExtensionWithFacts(reproducible=True)
    self._addFactToLockfile(extension_id, '1.27.0', {'hash': 'hola'})

    _, _, stderr = self.RunBazel(
        ['build', '@hello//:all', '--lockfile_mode=update']
    )
    self.assertIn('lockfile_ext is being evaluated', '\n'.join(stderr))

    with open(self.Path('MODULE.bazel.lock'), 'r') as f:
      lockfile = json.loads(f.read().strip())
    self.assertEqual(
        lockfile['facts'][extension_id],
        {'1.25.0': {'hash': 'olleh'}, '1.26.1': {'hash': 'hello'}},
    )
```

**File:** src/test/py/bazel/bzlmod/bazel_lockfile_test.py (L3783-3796)
```python
  def testDeletedFactsDetectedByErrorModeForReproducibleExtension(self):
    """ERROR mode reruns a reproducible extension on edited facts and fails.

    Regression test for https://github.com/bazelbuild/bazel/issues/29161
    """
    extension_id = self._setUpExtensionWithFacts(reproducible=True)
    self._dropFactsFromLockfile(extension_id, fact_key='1.25.0')

    exit_code, stdout, stderr = self.RunBazel(
        ['build', '@hello//:all', '--lockfile_mode=error'], allow_failure=True
    )
    stderr = ''.join(stderr)
    self.AssertExitCode(exit_code, 48, stderr, stdout)
    self.assertIn('has changed its facts', stderr)
```

**File:** docs/versions/8.6.0/external/extension.mdx (L259-280)
```text
If your extension relies on effectively immutable data obtained from outside
the build, most commonly from the network, but you don't have a checksum
available to guard the download, consider using the `facts` parameter of
[`extension_metadata`](/versions/8.6.0/rules/lib/builtins/module_ctx#extension_metadata) to
persistently record such data and thus allow your extension to become
reproducible. `facts` is expected to be a dictionary with string keys and
arbitrary JSON-like Starlark values that is always persisted in the lockfile and
available to future evaluations of the extension via the
[`facts`](/versions/8.6.0/rules/lib/builtins/module_ctx#facts) field of `module_ctx`.

`facts` are not invalidated even when the code of your module extension changes,
so be prepared to handle the case where the structure of `facts` changes.
Bazel also assumes that two different `facts` dicts produced by two different
evaluations of the same extension can be shallowly merged (i.e., as if by using
the `|` operator on two dicts). This is partially enforced by `module_ctx.facts`
not supporting enumeration of its entries, just lookups by key.

An example of using `facts` would be to record a mapping from version numbers of
some SDK to the an object containing the download URL and checksum of that
version. The first time the extension is evaluated, it can fetch this mapping
from the network, but on later evaluations it can use the mapping from `facts`
to avoid the network requests.
```

**File:** docs/external/lockfile.mdx (L83-93)
```text
## Lockfile Contents {#lockfile-contents}

The lockfile contains all the necessary information to determine whether the
project state has changed. It also includes the result of building the project
in the current state. The lockfile consists of two main parts:

1.   Hashes of all remote files that are inputs to module resolution.
2.   For each module extension, the lockfile includes inputs that affect it,
     represented by `bzlTransitiveDigest`, `usagesDigest` and other fields, as
     well as the output of running that extension, referred to as
     `generatedRepoSpecs`
```
