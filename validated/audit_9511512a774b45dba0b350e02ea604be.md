[1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** docs/versions/9.0.0/contribute/codebase.mdx (L1490-1497)
```text
There are various layers of caching since fetching a repository can be very
expensive:

1.  There is a cache for downloaded files that is keyed by their checksum
    (`RepositoryCache`). This requires the checksum to be available in the
    WORKSPACE file, but that's good for hermeticity anyway. This is shared by
    every Bazel server instance on the same workstation, regardless of which
    workspace or output base they are running in.
```

**File:** src/test/shell/bazel/bazel_repository_cache_test.sh (L211-238)
```shellscript
function test_cache_entry_exists() {
  setup_repository

  bazel fetch --repository_cache="$repo_cache_dir" //zoo:breeding-program >& $TEST_log \
    || echo "Expected fetch to succeed"
  if [ ! -f $repo_cache_dir/content_addressable/sha256/$sha256/file ]; then
    fail "the file was not cached successfully"
  fi
}

function test_fetch_value_with_existing_cache_and_no_network() {
  setup_repository

  # Manual cache injection
  cache_entry="$repo_cache_dir/content_addressable/sha256/$sha256"
  mkdir -p "$cache_entry"
  cp "$repo2_zip" "$cache_entry/file" # Artifacts are named uniformly as "file" in the cache
  http_archive_url="http://localhost:$nc_port/bleh"
  canonical_id_hash=$(printf "$http_archive_url" | sha256sum | cut -f 1 -d ' ')
  touch "$cache_entry/id-$canonical_id_hash"

  # Fetch without a server
  shutdown_server
  bazel fetch --repository_cache="$repo_cache_dir" //zoo:breeding-program >& $TEST_log \
      || echo "Expected fetch to succeed"

  expect_log "All external dependencies for the requested targets fetched successfully."
}
```

**File:** docs/versions/8.7.0/rules/lib/builtins/repository_ctx.mdx (L69-69)
```text
| `sha256` | [string](../core/string); default is `''`  The expected SHA-256 hash of the file downloaded. This must match the SHA-256 hash of the file downloaded. It is a security risk to omit the SHA-256 as remote files can change. At best omitting this field will make your build non-hermetic. It is optional to make development easier but should be set before shipping. If provided, the repository cache will first be checked for a file with the given hash; a download will only be attempted if the file was not found in the cache. After a successful download, the file will be added to the cache. |
```
