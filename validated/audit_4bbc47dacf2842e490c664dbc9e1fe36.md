Based on my research, I found a strong analog in the codebase's own `CHANGELOG.md`, though I was unable to locate the exact Java source file implementing this behavior (likely `DiskCacheClient` per the bug-class taxonomy) since the index does not contain its full contents. I'll note this limitation below. [1](#0-0) 

### Title
Disk cache no longer verifies digests on cache hit, enabling silent poisoning of a shared disk cache - (File: disk cache client implementation, referenced in `CHANGELOG.md`)

### Summary
The Sherlock report flags that `Oracle.getUnderlyingPrice` treats a governance-set "stable" value as always-trustworthy without re-validating it against the live market, so a stale/wrong value silently corrupts downstream logic. The same "trust the cached label without re-checking the underlying content" pattern exists in Bazel's disk cache: an explicit CHANGELOG entry in this repository states that Bazel stopped verifying the digest of a disk-cache entry when it is returned as a cache hit, even though this used to be governed by `--remote_verify_downloads` for both remote and disk caches [1](#0-0) .

### Finding Description
Bazel's disk cache (exercised in `src/test/shell/bazel/disk_cache_test.sh`) is a content-addressable store: entries are looked up and returned keyed by a requested digest, as demonstrated by the "1 disk cache hit" flows in `disk_cache_test.sh` and `remote_execution_test.sh` [2](#0-1) [3](#0-2) . The integrity guarantee of such a cache depends on the invariant that the bytes returned for a digest actually hash to that digest — i.e., the "cache key is total" and content cannot silently diverge from what it claims to be. The CHANGELOG explicitly documents that this verification was removed for disk-cache hits, decoupling the trusted label (the digest under which an entry is stored/retrieved) from a proof that the underlying bytes still match it [1](#0-0) . This mirrors the oracle's `stablePrice` short-circuit: once a "stable"/cached answer exists, the code stops asking "is this still true?" and just returns it.

Bazel's disk cache is commonly configured as a directory shared across multiple users/CI jobs (`--disk_cache=<dir>`), a scenario explicitly documented and tested, including garbage collection and cross-invocation reuse of `cas/` and `ac/` entries [4](#0-3) . An unprivileged party who can place or corrupt an entry under a given digest in that shared cache directory (e.g., another tenant on shared CI infrastructure building unrelated, untrusted branch content into the same cache path) can cause any other build that requests that digest to receive attacker-controlled bytes without Bazel re-hashing them to confirm the digest still matches.

### Impact Explanation
This breaks the core "integrity is binding" invariant of a content-addressable cache: a stored/retrieved artifact is supposed to be provably equal to the digest requesting it, but with verification skipped on disk-cache hits, corrupted or substituted content is served as if trusted. Because disk caches are shared build-to-build (and across users/CI jobs sharing a directory), this is cache poisoning that reaches other builds beyond the one that inserted the entry — matching the report's core concern that stale/untrusted "stable" state silently propagates incorrect assumptions into unrelated downstream consumers.

### Likelihood Explanation
Any environment using a shared `--disk_cache` directory (a common, documented performance optimization) is affected by default once this change ships, with no additional attacker capability required beyond the ability to write into or corrupt that shared directory — which is exactly the kind of "content the victim's build consumes but doesn't control" scenario the bug class targets.

### Recommendation
Re-verify the digest of disk-cache entries on every hit before treating them as trusted (restore `--remote_verify_downloads`-equivalent behavior for the disk cache), rather than trusting a cache key match alone.

### Proof of Concept
Not fully reproducible from the indexed contents: the source file that implements this disk-cache hit path (expected to be `DiskCacheClient` or similar, per the bug-class taxonomy) was not present in the accessible index, so I could not confirm the exact method or write a concrete JUnit/shell reproduction. The evidence for this finding is limited to the explicit behavioral statement in `CHANGELOG.md` [1](#0-0)  plus the disk-cache hit/miss test scaffolding in `disk_cache_test.sh` and `remote_execution_test.sh` that a Devin session with full repository access could extend into a reproduction (write a `cas/<digest>` entry with content that does NOT hash to `<digest>`, then trigger a disk-cache hit for that digest and confirm Bazel accepts the mismatched bytes without error).

Due to index size limits, some file contents (including the exact class implementing disk-cache retrieval) were not available to me. I recommend starting a Devin session with full repository access to confirm the precise class/method (likely under `src/main/java/com/google/devtools/build/lib/remote/disk/` or similar) and to build the reproducible test.

### Citations

**File:** CHANGELOG.md (L838-841)
```markdown
  - Bazel no longer verifies the digests of disk cache entries upon a
    cache hit. This honors the description but not the previous
    behavior of the `--remote_verify_downloads` flag, which in fact
    controlled digest verification for both remote and disk caches.
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

**File:** src/test/shell/bazel/disk_cache_test.sh (L131-150)
```shellscript
function test_garbage_collection() {
  local -r CACHE_DIR="${TEST_TMPDIR}/cache"
  rm -rf "$CACHE_DIR"

  mkdir -p a
  touch a/BUILD

  # Populate the disk cache with some fake entries totalling 4 MB in size.
  create_file_with_size_and_mtime "${CACHE_DIR}/cas/123" 1M "202401010100"
  create_file_with_size_and_mtime "${CACHE_DIR}/ac/456"  1M "202401010200"
  create_file_with_size_and_mtime "${CACHE_DIR}/cas/abc" 1M "202401010300"
  create_file_with_size_and_mtime "${CACHE_DIR}/ac/def"  1M "202401010400"

  # Run a build and request an immediate garbage collection.
  # Note that this build doesn't write anything to the disk cache.
  bazel build --disk_cache="$CACHE_DIR" \
    --disk_cache_gc_max_size=2M --disk_cache_gc_idle_delay=0 \
    //a:BUILD >& $TEST_log || fail "Expected build to succeed"

  # Give the idle task a bit of time to run.
```

**File:** src/test/shell/bazel/remote/remote_execution_test.sh (L1502-1517)
```shellscript
  # Case 3)
  #     disk_cache, remote_cache: remote_exec, disk_cache, remote_cache
  #          exist      notexist   no run      no update, no update
  # stop the worker to clear the remote cache and then restart it.
  # This ensures that if we hit the disk cache and it returns valid values
  # for FindMissingBLobs, the remote exec can still find it from the remote cache.

  stop_worker
  start_worker
  # need to reset flags after restarting worker [on new port]
  local grpc_flags="--remote_cache=grpc://localhost:${worker_port}"
  local remote_exec_flags="--remote_executor=grpc://localhost:${worker_port}"
  bazel clean
  bazel build $spawn_flags $testcase_flags $remote_exec_flags $grpc_flags $disk_flags //a:test &> $TEST_log \
      || fail "CASE 3 failed to build"
  expect_log "2 processes: 1 disk cache hit, 1 internal." "CASE 3: unexpected action line [[$(grep processes $TEST_log)]]"
```
