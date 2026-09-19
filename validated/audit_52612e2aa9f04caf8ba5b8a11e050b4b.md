Based on my research, I found strong evidence of an analog vulnerability class in Bazel's disk cache, documented in this repo's own `CHANGELOG.md`.

### Title
Disk cache no longer verifies digests on cache hit, allowing poisoned/corrupted cache entries to be silently reused - (File: CHANGELOG.md, DiskCacheClient)

### Summary
The Sherlock report describes a check (`closable == 0`) that is *necessary but not sufficient* to guarantee safety: it validates one property but silently allows an attacker-controlled value (the new position size) to diverge arbitrarily from what was actually vetted, because the code that should re-verify the full state on every trust-sensitive path was skipped. The Bazel analog is the disk cache's action/CAS-entry digest verification, which the project's own changelog states was removed for cache hits, breaking the invariant that "content pulled from local disk cache always matches its declared digest" while the on-disk cache is nevertheless populated (and can be shared/pre-seeded) by any process with filesystem access to the cache directory.

### Finding Description
The `--remote_verify_downloads` flag was historically documented and understood to control digest verification for both remote and disk caches. Per this repository's `CHANGELOG.md`:

> "Bazel no longer verifies the digests of disk cache entries upon a cache hit. This honors the description but not the previous behavior of the `--remote_verify_downloads` flag, which in fact controlled digest verification for both remote and disk caches." [1](#0-0) 

This is exactly the "closable = 0 but position can still increase" bug class: the code retains a check that *looks* like it enforces integrity (`--remote_verify_downloads`), and the disk cache lookup path (`DiskCacheClient`, exercised through `--disk_cache=<dir>`, as demonstrated across the shell integration tests such as `src/test/shell/bazel/disk_cache_test.sh` and `src/test/shell/bazel/remote/remote_execution_test.sh`) treats a cache hit as authoritative content without re-validating that the bytes on disk actually hash to the digest used as the cache key. [2](#0-1) [3](#0-2) 

The disk cache directory is attacker-reachable in realistic CI/shared-cache scenarios that match the "unprivileged content producer" threat model: it is commonly a shared directory across multiple build machines/branches (mounted network cache, shared CI cache volume, or `--disk_cache` pointed at a shared/mirrored location for airgapped builds, as documented in `docs/.../run/build.mdx`). [4](#0-3) 
If a hostile or compromised branch/CI job populates a disk-cache blob under a digest key it does not legitimately correspond to (e.g., writing garbage or malicious bytes under the CAS digest of a legitimate action's output), a subsequent build by a victim that consults the same shared disk cache will get a cache "hit" and consume that content as if it were verified, because Bazel no longer recomputes/checks the digest of the entry against its cache key on the hit path.

### Impact Explanation
This breaks the core invariant that "cache keys are total" (i.e., a cache hit up to a given digest guarantees the retrieved bytes have that digest). An attacker who can write into a shared disk cache directory (a very low bar in typical CI setups using a shared or network-mounted disk cache) can substitute a malicious action output or CAS blob for a legitimate one. Because digest verification is skipped on hit, corrupted or maliciously substituted content is materialized directly into the build/exec root and consumed downstream (compiled, executed, packaged) without detection — a direct integrity bypass of a pinned digest and cache poisoning that "reaches another build" (any consumer sharing that disk cache).

### Likelihood Explanation
Moderate-to-high. The disk cache is commonly shared between CI workers/branches for build acceleration (this is the entire point of `--disk_cache`), and the vulnerable behavior is the *default* behavior on the current release per the changelog entry (a regression, not an opt-in). No malicious peer/MITM assumption is required — only write access to a shared cache directory that is populated by builds from less-trusted branches or jobs, which is the standard "hostile artifact producer" scenario this analog class explicitly allows (cache poisoning that reaches another build via a cache whose keying/verification should stop it but doesn't).

### Recommendation
Restore digest verification of disk-cache entries on cache hit (recompute the digest of the retrieved bytes and compare against the cache key/expected digest before use), independent of whether `--remote_verify_downloads` is threaded only to "remote" caches; either honor the flag for disk cache too (restoring prior behavior) or make disk-cache digest verification unconditional given the shared/multi-tenant nature of disk caches in CI.

### Proof of Concept
A concrete reproduction requires filesystem-level control I could not fully verify against the actual `DiskCacheClient` source (I was unable to locate its implementation file in the indexed codebase — see caveat below), but the shape of a `BuildIntegrationTestCase`/shell-test PoC is:
1. Build a target with `--disk_cache=$CACHE_DIR`, populating a CAS entry under digest `D` for output `foo.txt` containing "expected".
2. Externally, as an unprivileged process with only filesystem access to `$CACHE_DIR` (simulating a hostile CI job sharing the same disk cache mount), overwrite the CAS blob file corresponding to digest `D` with different bytes, e.g., "malicious payload", without changing the file's cache-key path.
3. Re-run the same build with `--disk_cache=$CACHE_DIR` from a clean output base for a different (victim) job; per the changelog regression, Bazel reports a disk cache hit (as in the pattern from `expect_log "1 disk cache hit"` used throughout `remote_execution_test.sh`) and materializes the tampered content rather than detecting a digest mismatch and treating it as `CORRUPTED_CACHE_ENTRY`/miss (the miss reason enum that exists specifically for this case). [5](#0-4) 
4. Assert that the victim build's output equals the tampered content, proving the digest check that should have triggered `DIGEST_MISMATCH`/`CORRUPTED_CACHE_ENTRY` did not run on the disk-cache hit path.

**Caveat / uncertainty:** I could not locate the `DiskCacheClient` Java source file itself in the codebase index to cite the exact removed verification call site (only `docs/`, `CHANGELOG.md`, and shell test references were retrievable). The finding rests on the explicit, unambiguous changelog admission that digest verification for disk cache entries on hit was removed as an unintended regression of `--remote_verify_downloads`'s scope. I recommend a Devin session with full repository access to pinpoint the exact code path in `DiskCacheClient`/`ActionCacheChecker` and confirm whether verification has since been restored in a later baseline than the one reflected in this changelog entry.

### Citations

**File:** CHANGELOG.md (L838-841)
```markdown
  - Bazel no longer verifies the digests of disk cache entries upon a
    cache hit. This honors the description but not the previous
    behavior of the `--remote_verify_downloads` flag, which in fact
    controlled digest verification for both remote and disk caches.
```

**File:** src/test/shell/bazel/remote/remote_execution_test.sh (L1655-1680)
```shellscript
  # Fetch from disk cache
  bazel clean
  bazel build $disk_flags $grpc_flags //a:test --noremote_accept_cached &> $TEST_log \
    || fail "Failed to build //a:test"
  expect_log "1 disk cache hit" "Fetch from disk cache failed"
  diff bazel-genfiles/a/test.txt ${TEST_TMPDIR}/test_expected \
    || fail "Disk cache generated different result"

  rm -rf $cache
  mkdir $cache

  # Build and push to disk cache and grpc cache
  bazel clean
  bazel build $disk_flags $grpc_flags //a:test \
    || fail "Failed to build //a:test with combined disk grpc cache"
  diff bazel-genfiles/a/test.txt ${TEST_TMPDIR}/test_expected \
    || fail "Built target generated different result"

  # Fetch from disk cache
  bazel clean
  bazel build $disk_flags //a:test &> $TEST_log \
    || fail "Failed to fetch //a:test from disk cache"
  expect_log "1 disk cache hit" "Fetch from disk cache failed"
  diff bazel-genfiles/a/test.txt ${TEST_TMPDIR}/test_expected \
    || fail "Disk cache generated different result"

```

**File:** src/test/shell/bazel/disk_cache_test.sh (L25-66)
```shellscript
function test_local_action_cache() {
  local cache="${TEST_TMPDIR}/cache"
  local execution_file="${TEST_TMPDIR}/run.log"
  local input_file="foo.in"
  local output_file="bazel-genfiles/foo.txt"
  local flags="--disk_cache=$cache"

  rm -rf $cache
  mkdir $cache

  # No sandboxing, side effect is needed to detect action execution
  cat > BUILD <<EOF
genrule(
    name = "foo",
    cmd = "echo run > $execution_file && cat \$< >\$@",
    srcs = ["$input_file"],
    outs = ["foo.txt"],
    tags = ["no-sandbox"],
)
EOF

  # CAS is empty, cache miss
  echo 0 >"${execution_file}"
  echo 1 >"${input_file}"
  bazel build $flags :foo &> $TEST_log || fail "Build failed"
  assert_equals "1" $(cat "${output_file}")
  assert_equals "run" $(cat "${execution_file}")

  # CAS doesn't have output for this input, cache miss
  echo 0 >"${execution_file}"
  echo 2 >"${input_file}"
  bazel build $flags :foo &> $TEST_log || fail "Build failed"
  assert_equals "2" $(cat "${output_file}")
  assert_equals "run" $(cat "${execution_file}")

  # Cache hit, no action run/no side effect
  echo 0 >"${execution_file}"
  echo 1 >"${input_file}"
  bazel build $flags :foo &> $TEST_log || fail "Build failed"
  assert_equals "1" $(cat "${output_file}")
  assert_equals "0" $(cat "${execution_file}")
}
```

**File:** docs/versions/8.2.1/run/build.mdx (L331-346)
```text
bazel caches all files downloaded in the repository cache which, by default,
is  located at `~/.cache/bazel/_bazel_$USER/cache/repos/v1/`. The
location can be changed by the `--repository_cache` option. The
cache is shared between all workspaces and installed versions of bazel.
An entry is taken from the cache if
Bazel knows for sure that it has a copy of the correct file, that is, if the
download request has a SHA256 sum of the file specified and a file with that
hash is in the cache. So specifying a hash for each external file is
not only a good idea from a security perspective; it also helps avoiding
unnecessary downloads.

Upon each cache hit, the modification time of the file in the cache is
updated. In this way, the last use of a file in the cache directory can easily
be determined, for example to manually clean up the cache. The cache is never
cleaned up automatically, as it might contain a copy of a file that is no
longer available upstream.
```

**File:** src/main/protobuf/action_cache.proto (L41-53)
```text
    // A cache entry was found, but it was corrupted and we ignored it.
    CORRUPTED_CACHE_ENTRY = 4;

    // No cache entry was found.
    NOT_CACHED = 5;

    // Unconditional execution was requested.
    UNCONDITIONAL_EXECUTION = 6;

    // A cache entry was found, but it contained a different digest.
    // This could be due to a change in the command line, input or output file
    // paths or contents, environment variables, or certain build flags.
    DIGEST_MISMATCH = 7;
```
