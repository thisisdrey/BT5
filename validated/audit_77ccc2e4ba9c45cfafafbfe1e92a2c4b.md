Based on my research, I found a concrete, documented analog in Bazel's own changelog and code history: the removal of digest verification for local disk-cache hits.

### Title
Disk cache poisoning via unverified digests on cache hit - (File: `src/main/java/com/google/devtools/build/lib/remote/disk/DiskCacheClient.java`, behavior documented in `CHANGELOG.md`)

### Summary
The `--disk_cache` (and combined disk+remote cache) implementation in Bazel stores and serves content-addressed blobs keyed by their digest, analogous to the vulnerable contract's `holdTokenBalance` — a value read from shared, externally-writable storage and trusted without re-derivation. The changelog for this repository explicitly documents that a protective invariant was removed: [1](#0-0) . This means content read from a disk-cache entry on a hit is no longer re-hashed and compared against the requested digest before being trusted as that digest's content, mirroring the original bug's core flaw of trusting a shared/externally-influenced value without validating it against the expected/derived value at time of use.

### Finding Description
Bazel's disk cache (used standalone via `--disk_cache` or combined with a remote cache) stores blobs on a shared local filesystem path, keyed by digest, exactly like `RepositoryCache`'s `content_addressable/sha256/<hash>/file` layout described in `docs/run/build.mdx` [2](#0-1) . The `--remote_verify_downloads` flag was historically responsible for controlling digest re-verification for *both* remote and disk caches. According to the CHANGELOG, this was changed so that "Bazel no longer verifies the digests of disk cache entries upon a cache hit," which "honors the description but not the previous behavior" of that flag [1](#0-0) .

This directly parallels the Solidity bug class: a piece of state that is nominally derived from a trusted computation (the sha256/integrity digest that names the cache entry) is instead read back and used without re-verification, so any process capable of writing into that shared disk-cache directory (a multi-user/shared cache directory, a CI runner reusing a persistent `--disk_cache` volume across untrusted branch builds, etc.) can plant or corrupt a blob under a given digest key, and subsequent unrelated builds that hit that same digest will silently consume the tampered bytes as if they had been verified — a cache-poisoning condition that reaches other builds.

### Impact Explanation
If a shared `--disk_cache` directory (e.g., a CI-wide cache volume mounted across builds from different, less-trusted branches/PRs) contains a corrupted or attacker-substituted entry, any build — including builds from the trusted, privileged branch — that references the same digest (e.g., identical source content, or a colliding/forced digest) will get served the corrupted content on a "disk cache hit" without any content re-hash to catch the mismatch. This can silently corrupt build outputs or actions that are otherwise assumed to be content-addressed and therefore safe to trust blindly.

### Likelihood Explanation
The digest is used purely as a lookup key; before this change, an actual hash-check on read acted as the binding invariant guaranteeing that "digest X" and "the bytes retrieved for X" always matched, which is the same invariant relied upon in `RepositoryCache`/`DownloadCache` documentation ("An entry is taken from the cache if Bazel knows for sure that it has a copy of the correct file") [3](#0-2) . The changelog confirms this invariant was intentionally dropped for disk-cache hits, and this is a default, always-on code path for anyone using `--disk_cache`, not an opt-in flag — meaning any environment sharing a disk-cache directory across trust boundaries is affected by default.

### Recommendation
Restore re-verification of the digest against the actual bytes read from a disk-cache entry on every cache hit (not just relying on the filename/path as ground truth), or reintroduce `--remote_verify_downloads`-gated verification specifically for the disk cache path, so that any entry whose content does not hash to its key name is treated as a cache miss (and ideally evicted) rather than trusted.

### Proof of Concept
A `src/test/shell/bazel/remote/build_without_the_bytes_test.sh`-style JUnit/shell reproduction would: (1) run a build with `--disk_cache=$CACHEDIR` to populate an entry for digest D with correct content; (2) directly overwrite the on-disk blob file for digest D in `$CACHEDIR` with different, attacker-chosen bytes (simulating a hostile write to a shared cache volume); (3) run a second build that is expected to hit the disk cache for digest D; (4) assert that the resulting action inputs/outputs still contain the original hashed content (i.e., that Bazel detects and rejects the mismatch) — which, per the documented behavior change, currently fails because the mismatch is not detected on cache hit.

**Uncertainty note:** I was unable to retrieve the full Java source of `DiskCacheClient.java` (or `DiskAndRemoteCacheClient`/`GetActionResult` disk-cache read paths) through the available search tools — only the CHANGELOG entry and shell-test behavior around disk-cache hits were indexed. The exact code location and control flow of the read/verify logic (and whether any residual verification exists elsewhere, e.g., in `ActionCacheChecker` metadata checks) could not be directly confirmed from source. A full Devin session with repository file access would be needed to pinpoint the exact method and line removed, and to build a precise JUnit-level PoC.

### Citations

**File:** CHANGELOG.md (L838-841)
```markdown
  - Bazel no longer verifies the digests of disk cache entries upon a
    cache hit. This honors the description but not the previous
    behavior of the `--remote_verify_downloads` flag, which in fact
    controlled digest verification for both remote and disk caches.
```

**File:** docs/run/build.mdx (L331-339)
```text
is  located at `~/.cache/bazel/_bazel_$USER/cache/repos/v1/`. The
location can be changed by the `--repository_cache` option. The
cache is shared between all workspaces and installed versions of bazel.
An entry is taken from the cache if
Bazel knows for sure that it has a copy of the correct file, that is, if the
download request has a SHA256 sum of the file specified and a file with that
hash is in the cache. So specifying a hash for each external file is
not only a good idea from a security perspective; it also helps avoiding
unnecessary downloads.
```
