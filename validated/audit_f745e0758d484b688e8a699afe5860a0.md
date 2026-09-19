### Title
Module-extension `facts` in `MODULE.bazel.lock` are trusted as an integrity record without verification, allowing persistent poisoning with no invalidation path - (File: `src/test/py/bazel/bzlmod/bazel_lockfile_test.py`, `docs/external/extension.mdx`)

### Summary
Bazel's bzlmod `facts` mechanism is designed to let a module extension persist "effectively immutable data obtained from outside the build" (typically a URL + checksum for downloads that aren't guarded by a `sha256`) into `MODULE.bazel.lock`, and reuse it on every subsequent build instead of re-fetching. [1](#0-0)  Because the whole point of `facts` is to stand in for a checksum precisely when no checksum exists, Bazel does not independently verify the persisted value against anything — it is accepted as-is whenever `--lockfile_mode=update` (the default) sees it in the lockfile, even if the lockfile content was not produced by a local evaluation of the extension. This is the same fault pattern as the Sablier `MerkleLockup` clawback bug: once bad state is committed, there is no correction/expiration path available to the victim, only to whoever controls the extension's own versioning.

### Finding Description
The `facts` dict returned via `ctx.extension_metadata(facts=...)` is persisted verbatim in `MODULE.bazel.lock` and handed back to the extension on the next evaluation via `module_ctx.facts`, without any hash or provenance check tying the stored value to a real fetch. [2](#0-1) 

Bazel's own regression test explicitly documents that when the workspace lockfile's `facts` entry for a non-reproducible extension is modified externally — the comment gives the concrete scenario "due to a branch switch or pull that brings in facts produced by an evaluation of the extension on a different machine" — `--lockfile_mode=update` (default mode) keeps the new facts and merges them into the lockfile without ever re-running the extension or validating the new value against the network resource it purports to describe: [3](#0-2) 

The helper that performs this simulated "external" edit is `_addFactToLockfile`, which the test suite itself frames as modeling "the workspace lockfile changing underneath a warm output base": [4](#0-3) 

A concrete usage pattern shown in `testFactsInReproducibleExtension` demonstrates that facts routinely carry integrity-relevant data (a per-artifact `hash`) that is fed straight into a `repository_rule` attribute without re-verification: [5](#0-4) 

The offline lockfile-merge tool (`scripts/bazel-lockfile-merge.jq`, used to reconcile `MODULE.bazel.lock` files from concurrent CI branches) reinforces the same trust assumption: it shallow-merges `facts` entries from multiple lockfiles keyed only by extension id and `factsVersion`, with no cross-check against the actual upstream resource: [6](#0-5) 

Documentation confirms `facts` are never invalidated by code changes and are only discarded when the extension author bumps `facts_version` — a decision the victim of a poisoned lockfile has no control over: [7](#0-6) 

### Impact Explanation
An unprivileged attacker who can get content into a checked-in `MODULE.bazel.lock` that a victim's CI later builds from (e.g., by pushing to or opening a PR against an untrusted branch, or by supplying a bad response the first time a `facts`-recording extension fetches metadata over the network, since by definition that fetch has no `sha256`/`integrity` guard) can inject an attacker-chosen "hash"/URL pair into the `facts` section for a given module extension. Because `--lockfile_mode=update` merges and trusts pre-existing `facts` values without re-fetching or verifying them, this poisoned entry:
- Persists indefinitely across builds and machines (it is explicitly designed to skip network access on future evaluations).
- Propagates through the described merge tooling into other developers'/CI's lockfiles.
- Feeds directly into `repository_rule` attributes (e.g., a `hash` attr as shown in the test), which an extension may use to select or validate a downloaded artifact — meaning bad "facts" can effectively defeat the very checksum protection the mechanism was created to substitute for.
- Has no clawback/expiration path: the only correction mechanism is the extension author bumping `facts_version`, which is out of the victim's control, exactly mirroring the Sablier bug where recovery was gated on the misconfiguring party's own action (or an expiration nobody set).

### Likelihood Explanation
Requires an extension that uses `facts` for un-checksummed metadata (an increasingly encouraged pattern per Bazel's own extension-authoring docs) and an attacker able to place content into a lockfile a victim's build consumes (branch/PR content or first-fetch response). Both conditions are realistic in open-source/CI environments and match the allowed unprivileged-attacker model (no MITM/malicious-peer premise required — a hostile lockfile entry alone triggers the effect at default flags).

### Recommendation
- Do not silently trust `facts` values present in an out-of-context lockfile edit; require that non-reproducible extensions' facts changes without corresponding input changes trigger a warning (or re-evaluation gated behind an explicit `--lockfile_mode=refresh`) rather than silent adoption under `update`.
- Where `facts` values are used as a substitute for a checksum (e.g., recording a `sha256`), consider validating them against the actual fetched artifact on first use in each fresh checkout rather than assuming lockfile content is trustworthy input.
- Document and provide an explicit "reject externally-modified facts" mode for security-sensitive CI usage, analogous to `--lockfile_mode=error`, that fails the build instead of merging unrecognized facts changes.

### Proof of Concept
The existing test `testExternallyUpdatedFactsKeptByUpdateModeForNonReproducibleExtension` is itself the reproducible proof: it edits `MODULE.bazel.lock`'s `facts` section out-of-band (simulating an attacker-controlled branch/pull) and shows that a subsequent `bazel build --lockfile_mode=update` accepts and permanently merges the injected fact without re-running the extension: [3](#0-2) 

Note: I was unable to locate/inspect the underlying Java implementation (`SingleExtensionEvalFunction` / `BazelLockFileFunction`) in this index — it is not indexed under this repository snapshot — so the exact Java call sites that read/merge `facts` during lockfile-mode evaluation could not be cited directly. The behavior is confirmed via the shipped integration test and the official documentation instead. If a byte-level trace into the Java lockfile-eval code is needed, a Devin session with full repo access would be required to inspect `src/main/java/com/google/devtools/build/lib/bazel/bzlmod/` directly.

### Citations

**File:** docs/external/extension.mdx (L258-273)
```text
If your extension relies on effectively immutable data obtained from outside
the build, most commonly from the network, but you don't have a checksum
available to guard the download, consider using the `facts` parameter of
[`extension_metadata`](/rules/lib/builtins/module_ctx#extension_metadata) to
persistently record such data and thus allow your extension to become
reproducible. `facts` is expected to be a dictionary with string keys and
arbitrary JSON-like Starlark values that is always persisted in the lockfile and
available to future evaluations of the extension via the
[`facts`](/rules/lib/builtins/module_ctx#facts) field of `module_ctx`.

`facts` are not invalidated even when the code of your module extension changes,
so be prepared to handle the case where the structure of `facts` changes.
Bazel also assumes that two different `facts` dicts produced by two different
evaluations of the same extension can be shallowly merged (i.e., as if by using
the `|` operator on two dicts). This is partially enforced by `module_ctx.facts`
not supporting enumeration of its entries, just lookups by key.
```

**File:** docs/external/extension.mdx (L281-287)
```text
If you need to make a backwards-incompatible change to the schema of the values
your extension stores in `facts`, set the
[`facts_version`](/rules/lib/globals/bzl#module_extension.facts_version)
parameter on `module_extension` to a higher integer than its previous value.
Bazel persists `facts_version` in the lockfile alongside the facts and discards
any persisted facts when the recorded version differs from the current one,
making sure your extension only ever observes facts produced by the same schema.
```

**File:** src/test/py/bazel/bzlmod/bazel_lockfile_test.py (L3067-3084)
```python
            '    if resource_name in ctx.facts:',
            '        return ctx.facts[resource_name]',
            '    print("Fetching metadata for {}...".format(resource_name))',
            '    return {"hash": resource_name[::-1]}',
            'def _mod_ext_impl(ctx):',
            '    print("Hello from the other side!")',
            '    metadata = {}',
            '    for repo in ["hello", "world"]:',
            '        metadata[repo] = _fetch_metadata(ctx, repo)',
            '        print("{}: hash={}".format(repo, metadata[repo]["hash"]))',
            '        repo_rule(',
            '            name = repo,',
            '            hash = metadata[repo]["hash"],',
            '        )',
            '    return ctx.extension_metadata(',
            '        reproducible = True,',
            '        facts = metadata,',
            '    )',
```

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

**File:** src/test/py/bazel/bzlmod/bazel_lockfile_test.py (L3725-3756)
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
    """
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

**File:** scripts/bazel-lockfile-merge.jq (L59-73)
```text
    | {
        lockFileVersion: $maxVersion,
        registryFileHashes: shallow_merge(.registryFileHashes),
        selectedYankedVersions: shallow_merge(.selectedYankedVersions),
        # Group extension results by extension ID across all lockfiles with
        # shallowly merged factors map, then shallowly merge the results.
        moduleExtensions:  (map(.moduleExtensions | to_entries)
                           | flatten
                           | if length > 0 then group_by(.key) | shallow_merge({(.[0].key): shallow_merge(.value)}) else {} end),
        # Group facts by extension ID across all lockfiles (already filtered to
        # the latest factsVersion above) and shallowly merge their dicts.
        facts: (if any(has("facts")) then
                  map(.facts // {} | to_entries) | flatten |
                  if length > 0 then group_by(.key) | shallow_merge({(.[0].key): shallow_merge(.value)}) else {} end
                else null end),
```
