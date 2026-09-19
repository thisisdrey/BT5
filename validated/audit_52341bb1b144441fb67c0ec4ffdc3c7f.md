Note: I was unable to locate the actual Java source files for `RepoContentsCache` / `RemoteRepoContentsCacheImpl` in the index (searches for `*.java` matches returned nothing — likely excluded by index size limits). My analysis below is based only on the observable behavior documented in the shell/py integration tests (`src/test/py/bazel/bzlmod/repo_contents_cache_test.py`, `src/test/py/bazel/bzlmod/remote_repo_contents_cache_test.py`, `src/test/shell/integration/sandboxing_test.sh`) and the `repository_ctx.repo_metadata(reproducible=...)` documentation. I could not directly inspect the key-computation or write-path code to fully confirm there is no last-writer/first-writer race protection at the implementation level.

### Title
Repo contents cache (local and remote) is keyed by declared/recorded inputs, not by content hash, allowing a first-fetch to poison the cache for all later builds sharing it - ([File: src/test/py/bazel/bzlmod/repo_contents_cache_test.py])

### Summary
Bazel's repo contents cache (`--repo_contents_cache`, and its remote counterpart backed by `--remote_cache` when `--experimental_remote_repo_contents_cache` is enabled) stores the entire output directory produced by a `repository_rule` implementation whenever that implementation returns `repo_metadata(reproducible=True)`. The cache key is derived only from the rule's declared attributes and its recorded inputs (watched files, environment variables) [1](#0-0) , not from a checksum of the actual fetched/produced content. This is structurally analogous to an unguarded `initialize()`: whichever party's fetch runs first for a given (attrs, recorded-inputs) key gets its output cached and trusted by every subsequent build that shares the cache and requests the same key — with no verification that the content matches anything else.

### Finding Description
For `--repository_cache`, entries are addressed by the declared `sha256`/`integrity` of the downloaded artifact, so a hostile origin cannot poison the cache without the resulting bytes matching a pinned digest [2](#0-1) , and this is explicitly documented as content-addressable and hash-gated [3](#0-2) .

The **repo contents cache** is different: it caches the *whole materialized repository directory* produced by a `repository_rule`'s implementation function (which may run arbitrary `execute()` commands, clone git repos, call `download()` without a hash, etc.), gated only by the rule declaring `repo_metadata(reproducible=True)`. The tests show the cache hit/miss behavior is driven purely by whether the rule's **predeclared attributes** and **recorded inputs** (files watched via `rctx.watch`, env vars via `rctx.getenv`/`environ`) are unchanged [4](#0-3) [5](#0-4) . Nothing in this key incorporates a checksum of the produced repository content itself — the value written under a given key on first fetch is what every future build reads back for that key [6](#0-5) .

The remote variant explicitly shares this cache via `--remote_cache=grpc://...`, i.e., across machines and (in many CI setups) across branches/pipelines that write to a common remote cache endpoint [7](#0-6) . Because the key is a function only of the repo rule's declared/recorded inputs and not of the fetched content, if an attacker-controlled build (e.g., an untrusted branch/PR pipeline that shares the same `--remote_cache` with the trusted mainline, or someone with the ability to seed the local `--repo_contents_cache` directory) runs first for a given `(repo_rule, attrs, recorded-inputs)` combination — for example, because their repo rule's implementation intentionally omits a real checksum (`download()` with no `sha256`, or an `execute()`-based fetch) — their materialized repo directory is written into the shared cache under that key. Every later build (including the trusted/legitimate one, if it declares the same rule with the same attributes) will get a silent cache hit and reuse the attacker's content without any subsequent re-fetch or verification, exactly like a front-run `initialize()`: the first writer for the "slot" wins permanently, and the legitimate owner's real content is never consulted because the slot is already considered populated.

### Impact Explanation
This allows an unprivileged party who can race a write into a shared repo contents cache (local or, more realistically, a shared remote cache used across CI jobs/branches) to have malicious repository content (arbitrary `BUILD`, `.bzl`, or source files placed into the "external" repo directory) served to every subsequent build that resolves the same repository-rule invocation and shares that cache — a cache-poisoning affecting builds that never contacted the attacker's origin at all. Because the cache is keyed by declared attributes/recorded inputs rather than by a content digest, there is no cryptographic binding preventing this substitution, unlike the classic `--repository_cache` SHA256-keyed download cache.

### Likelihood Explanation
Requires: (1) a repository rule marked `reproducible=True` whose implementation does not itself pin every input via a checksum (common for rules using `execute()`, `git_repository`-style fetches, or `download()`/`download_and_extract()` without `sha256`/`integrity`), and (2) a repo contents cache (local directory or remote cache endpoint) shared between the attacker's build context and the victim's. The remote variant is explicitly designed to be shared across machines/CI via `--remote_cache`, which is a realistic and common configuration, making the race plausible in CI setups where untrusted branches and trusted branches share the same remote cache backend.

### Recommendation
Bind repo-contents-cache entries to a content digest of the actual materialized repository (in addition to the declared/recorded-input key), or require that only rules whose entire input surface is itself checksum-verified (e.g., all downloads use `sha256`/`integrity`) qualify for `reproducible=True` caching; alternatively, restrict writes to the shared/remote repo contents cache to trusted principals and treat any cache hit as advisory unless the recorded inputs it was keyed on are themselves fully verified (checksummed) rather than merely "recorded."

### Proof of Concept
A reproducible-shell/JUnit-style scenario, following the existing test harness pattern in `src/test/py/bazel/bzlmod/remote_repo_contents_cache_test.py`:
1. Configure two independent Bazel workspaces (simulating "attacker CI branch" and "victim mainline") both pointed at the same `--remote_cache=grpc://localhost:<port>` with `--experimental_remote_repo_contents_cache` enabled, mirroring the `RemoteRepoContentsCacheTest.setUp` configuration [7](#0-6) .
2. Define a `repository_rule` with a fixed attribute set (no hash pinned) whose implementation writes attacker-controlled `BUILD`/source content and returns `rctx.repo_metadata(reproducible=True)`, analogous to the `_repo_impl` pattern in `testCachedAfterCleanExpunge` [8](#0-7) .
3. In the "attacker" workspace, run `bazel build @my_repo//:haha` first so the malicious output is uploaded to the shared remote cache under the key derived solely from the rule's declared attrs.
4. In the "victim" workspace (same rule/attrs, different/legitimate intended content), run the same build after a `clean --expunge`; assert (as the existing `assertNotIn('JUST FETCHED', ...)` pattern shows) that the repo rule is never re-run and the victim's build silently receives the attacker's materialized directory instead of running its own fetch logic — demonstrating cache-poisoning across build contexts sharing the same cache key without content verification.

### Citations

**File:** src/test/py/bazel/bzlmod/repo_contents_cache_test.py (L171-216)
```python
  def testNotCachedWhenPredeclaredInputsChange(self):
    self.ScratchFile(
        'MODULE.bazel',
        [
            'repo = use_repo_rule("//:repo.bzl", "repo")',
            'repo(name = "my_repo", data = 1)',
        ],
    )
    self.ScratchFile('BUILD.bazel')
    self.ScratchFile(
        'repo.bzl',
        [
            'def _repo_impl(rctx):',
            '  rctx.file("BUILD", "filegroup(name=\'haha\')")',
            '  print("JUST FETCHED")',
            '  return rctx.repo_metadata(reproducible=True)',
            'repo = repository_rule(_repo_impl, attrs={"data":attr.int()})',
        ],
    )

    # First fetch: not cached
    _, _, stderr = self.RunBazel(['build', '@my_repo//:haha'])
    self.assertIn('JUST FETCHED', '\n'.join(stderr))

    # Change predeclared inputs: not cached
    self.ScratchFile(
        'MODULE.bazel',
        [
            'repo = use_repo_rule("//:repo.bzl", "repo")',
            'repo(name = "my_repo", data = 2)',
        ],
    )
    _, _, stderr = self.RunBazel(['build', '@my_repo//:haha'])
    self.assertIn('JUST FETCHED', '\n'.join(stderr))

    # Change back to previous predeclared inputs: cached (even after expunging)
    self.RunBazel(['clean', '--expunge'])
    self.ScratchFile(
        'MODULE.bazel',
        [
            'repo = use_repo_rule("//:repo.bzl", "repo")',
            'repo(name = "my_repo", data = 1)',
        ],
    )
    _, _, stderr = self.RunBazel(['build', '@my_repo//:haha'])
    self.assertNotIn('JUST FETCHED', '\n'.join(stderr))
```

**File:** src/test/py/bazel/bzlmod/repo_contents_cache_test.py (L218-253)
```python
  def testNotCachedWhenRecordedInputsChange(self):
    self.ScratchFile(
        'MODULE.bazel',
        [
            'repo = use_repo_rule("//:repo.bzl", "repo")',
            'repo(name = "my_repo")',
        ],
    )
    self.ScratchFile('BUILD.bazel')
    self.ScratchFile(
        'repo.bzl',
        [
            'def _repo_impl(rctx):',
            '  rctx.file("BUILD", "filegroup(name=\'haha\')")',
            '  rctx.watch(Label("@//:data.txt"))',
            '  print("JUST FETCHED")',
            '  return rctx.repo_metadata(reproducible=True)',
            'repo = repository_rule(_repo_impl)',
        ],
    )

    # First fetch: not cached
    self.ScratchFile('data.txt', ['one'])
    _, _, stderr = self.RunBazel(['build', '@my_repo//:haha'])
    self.assertIn('JUST FETCHED', '\n'.join(stderr))

    # Change recorded inputs: not cached
    self.ScratchFile('data.txt', ['two'])
    _, _, stderr = self.RunBazel(['build', '@my_repo//:haha'])
    self.assertIn('JUST FETCHED', '\n'.join(stderr))

    # Change back to previous recorded inputs: cached (even after expunging)
    self.RunBazel(['clean', '--expunge'])
    self.ScratchFile('data.txt', ['one'])
    _, _, stderr = self.RunBazel(['build', '@my_repo//:haha'])
    self.assertNotIn('JUST FETCHED', '\n'.join(stderr))
```

**File:** src/test/py/bazel/bzlmod/repo_contents_cache_test.py (L255-293)
```python
  def testNotCachedWhenRecordedInputsChange_envVar(self):
    self.ScratchFile(
        'MODULE.bazel',
        [
            'repo = use_repo_rule("//:repo.bzl", "repo")',
            'repo(name = "my_repo")',
        ],
    )
    self.ScratchFile('BUILD.bazel')
    self.ScratchFile(
        'repo.bzl',
        [
            'def _repo_impl(rctx):',
            '  rctx.file("BUILD", "filegroup(name=\'haha\')")',
            '  rctx.getenv("LOLOL")',
            '  print("JUST FETCHED")',
            '  return rctx.repo_metadata(reproducible=True)',
            'repo = repository_rule(_repo_impl)',
        ],
    )

    # First fetch: not cached
    _, _, stderr = self.RunBazel(
        ['build', '@my_repo//:haha'], env_add={'LOLOL': 'lol'}
    )
    self.assertIn('JUST FETCHED', '\n'.join(stderr))

    # Change recorded inputs: not cached
    _, _, stderr = self.RunBazel(
        ['build', '@my_repo//:haha'], env_add={'LOLOL': 'kek'}
    )
    self.assertIn('JUST FETCHED', '\n'.join(stderr))

    # Change back to previous recorded inputs: cached (even after expunging)
    self.RunBazel(['clean', '--expunge'])
    _, _, stderr = self.RunBazel(
        ['build', '@my_repo//:haha'], env_add={'LOLOL': 'lol'}
    )
    self.assertNotIn('JUST FETCHED', '\n'.join(stderr))
```

**File:** docs/versions/8.7.0/rules/lib/builtins/repository_ctx.mdx (L69-76)
```text
| `sha256` | [string](../core/string); default is `''`  The expected SHA-256 hash of the file downloaded. This must match the SHA-256 hash of the file downloaded. It is a security risk to omit the SHA-256 as remote files can change. At best omitting this field will make your build non-hermetic. It is optional to make development easier but should be set before shipping. If provided, the repository cache will first be checked for a file with the given hash; a download will only be attempted if the file was not found in the cache. After a successful download, the file will be added to the cache. |
| `executable` | [bool](../core/bool); default is `False`  Set the executable flag on the created file, false by default. |
| `allow_fail` | [bool](../core/bool); default is `False`  If set, indicate the error in the return value instead of raising an error for failed downloads. |
| `canonical_id` | [string](../core/string); default is `''`  If set, restrict cache hits to those cases where the file was added to the cache with the same canonical id. By default caching uses the checksum (`sha256` or `integrity`). |
| `auth` | [dict](../core/dict); default is `{}`  An optional dict specifying authentication information for some of the URLs. |
| `headers` | [dict](../core/dict); default is `{}`  An optional dict specifying http headers for all URLs. |
| `integrity` | [string](../core/string); default is `''`  Expected checksum of the file downloaded, in Subresource Integrity format. This must match the checksum of the file downloaded. It is a security risk to omit the checksum as remote files can change. At best omitting this field will make your build non-hermetic. It is optional to make development easier but should be set before shipping. If provided, the repository cache will first be checked for a file with the given checksum; a download will only be attempted if the file was not found in the cache. After a successful download, the file will be added to the cache. |
| `block` | [bool](../core/bool); default is `True`  If set to false, the call returns immediately and instead of the regular return value, it returns a token with one single method, wait(), which blocks until the download is finished and returns the usual return value or throws as usual. |
```

**File:** docs/versions/8.5.1/run/build.mdx (L335-340)
```text
An entry is taken from the cache if
Bazel knows for sure that it has a copy of the correct file, that is, if the
download request has a SHA256 sum of the file specified and a file with that
hash is in the cache. So specifying a hash for each external file is
not only a good idea from a security perspective; it also helps avoiding
unnecessary downloads.
```

**File:** src/test/py/bazel/bzlmod/remote_repo_contents_cache_test.py (L35-49)
```python
  def setUp(self):
    test_base.TestBase.setUp(self)
    self._worker_port = self.StartRemoteWorker()
    self.ScratchFile(
        '.bazelrc',
        [
            'startup --experimental_remote_repo_contents_cache',
            # Only use the remote repo contents cache.
            'common --repo_contents_cache=',
            'common --remote_cache=grpc://localhost:' + str(self._worker_port),
            'common --auth_enabled=false',
            'common --remote_timeout=3600s',
            'common --verbose_failures',
        ],
    )
```

**File:** src/test/py/bazel/bzlmod/remote_repo_contents_cache_test.py (L67-108)
```python
  def testCachedAfterCleanExpunge(self):
    self.ScratchFile(
        'MODULE.bazel',
        [
            'repo = use_repo_rule("//:repo.bzl", "repo")',
            'repo(name = "my_repo")',
        ],
    )
    self.ScratchFile('BUILD.bazel')
    self.ScratchFile(
        'repo.bzl',
        [
            'def _repo_impl(rctx):',
            '  rctx.file("BUILD", "filegroup(name=\'haha\')")',
            '  print("JUST FETCHED")',
            '  return rctx.repo_metadata(reproducible=True)',
            'repo = repository_rule(_repo_impl)',
        ],
    )

    repo_dir = self.RepoDir('my_repo')

    # First fetch: not cached
    _, _, stderr = self.RunBazel(['build', '@my_repo//:haha'])
    self.assertIn('JUST FETCHED', '\n'.join(stderr))
    self.assertTrue(os.path.exists(os.path.join(repo_dir, 'BUILD')))

    # After expunging: cached
    self.RunBazel(['clean', '--expunge'])
    _, _, stderr = self.RunBazel(['build', '@my_repo//:haha'])
    self.assertNotIn('JUST FETCHED', '\n'.join(stderr))
    self.assertFalse(os.path.exists(os.path.join(repo_dir, 'BUILD')))

    # After expunging, without using repo contents cache: not cached
    self.RunBazel(['clean', '--expunge'])
    _, _, stderr = self.RunBazel([
        '--noexperimental_remote_repo_contents_cache',
        'build',
        '@my_repo//:haha',
    ])
    self.assertIn('JUST FETCHED', '\n'.join(stderr))
    self.assertTrue(os.path.exists(os.path.join(repo_dir, 'BUILD')))
```
