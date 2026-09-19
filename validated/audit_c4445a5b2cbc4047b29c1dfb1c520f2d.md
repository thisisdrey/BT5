### Title
Delimiter-collision in `get_default_canonical_id` allows canonical-ID spoofing across differently-partitioned URL lists - (File: tools/build_defs/repo/cache.bzl)

### Summary
The Sherlock report flags `abi.encodePacked()` with multiple dynamic arguments in `LockingPositionManager.tokenURI` as a hash-collision risk because concatenating dynamic-length strings without a delimiter lets different logical inputs produce the identical packed/hashed value. The closest analog reachable in this Bazel checkout is `get_default_canonical_id()` in `tools/build_defs/repo/cache.bzl`, which builds the repository-cache "canonical ID" by naively joining a list of attacker-influenced URL strings with a single space character, with no escaping of spaces that may already occur inside an individual URL element.

### Finding Description
`get_default_canonical_id` computes the default canonical ID as: [1](#0-0) 

`" ".join(urls)` is a delimiter-based join over a list of dynamic strings (the `urls` attribute values, which are fully attacker-controlled content when a mirror or `urls=[...]` list is derived from an untrusted source such as a lockfile-adjacent module extension, a generated `MODULE.bazel.lock`, or repo-rule logic that assembles URLs from remote registry data). Because URLs may legitimately contain the space character (percent-encoded normally, but Starlark does not enforce this, and a malicious or misconfigured URL producer can supply a raw space), two semantically different `urls` lists can be joined into the exact same canonical-ID string, e.g.:
- `urls = ["http://good.example/a b.zip"]` → `"http://good.example/a b.zip"`
- `urls = ["http://good.example/a", "b.zip"]` → `"http://good.example/a b.zip"`

This is functionally identical to the encodePacked collision pattern: two distinct dynamic-length lists collapse to one packed representation with no distinguishing separator that can't itself appear inside an element.

### Impact Explanation
The documented purpose of the canonical ID is explicitly a *cache-gating* mechanism: "Bazel will not take the file from cache, unless it was added to the cache by a request with the same canonical ID," intended to "catch the common mistake of updating the URLs without also updating the hash" (see `CANONICAL_ID_DOC`, `tools/build_defs/repo/cache.bzl` lines 27-37, and `get_default_canonical_id`'s own anti-reordering comment at lines 65-70 showing the authors are already aware of one class of ambiguity — URL *order* — but not aware of the ambiguity from unescaped delimiter characters inside individual URL strings).

However, on inspection the actual `RepositoryCache` content-addressing in this checkout is primarily keyed by the declared `sha256`/integrity digest, not solely by canonical ID (per `docs/contribute/codebase.mdx`: "There is a cache for downloaded files that is keyed by their checksum (`RepositoryCache`)"). The canonical-ID join is a secondary/defense-in-depth check layered on top of the checksum-based content-addressable cache, not the primary integrity gate. I could not locate the Java-side consumer of `canonical_id` (e.g. `DownloadManager`/`HttpDownloader`) in the indexed portion of this repo to confirm exactly how a canonical-ID collision would be combined with the checksum check at fetch time — this file may not be fully indexed, so I cannot confirm whether a canonical-ID collision alone (with sha256 still validated) can bypass any integrity boundary or is purely a "which cache entries are considered fresh" convenience heuristic.

### Likelihood Explanation
Exploiting this reliably requires an attacker who can influence the `urls` list content (e.g. an operator's own module extension pulling URL fragments from an external, attacker-controlled registry response) to intentionally engineer a space-containing URL segment that collides with a legitimately split multi-URL list. This is a narrow, self-inflicted-config scenario rather than a generic remote attacker primitive, and given the outstanding checksum requirement for the actual cache content match, the practical security impact is limited to canonical-ID bookkeeping rather than a full integrity bypass.

### Recommendation
Use a canonicalization scheme that cannot collide across different partitions of the `urls` list — e.g., percent-encode or otherwise escape any delimiter characters found in individual URL strings before joining, or use a length-prefixed / `abi.encode`-style unambiguous serialization (such as `json.encode(urls)` or joining with a length-prefixed representation) instead of a bare `" ".join(urls)`.

### Proof of Concept
Not fully reproducible from the indexed contents of this repo: I found the vulnerable join logic in `tools/build_defs/repo/cache.bzl` but could not locate/inspect the Java consumer (`DownloadManager`/`RepositoryCache` equivalent) that reads `canonical_id` to confirm end-to-end exploitability or write a `src/test/shell/bazel` proof within the available context. A `src/test/shell/bazel/bazel_repository_cache_test.sh`-style test could be added that fetches two different `http_archive` invocations — one with a single space-containing URL and one with the same content split into two list entries — under `--repo_env=BAZEL_HTTP_RULES_URLS_AS_DEFAULT_CANONICAL_ID=1`, and observes whether the second fetch is served from the first's cache entry despite differing `urls` semantics; this was not executed here. [2](#0-1) [3](#0-2)

### Citations

**File:** tools/build_defs/repo/cache.bzl (L27-37)
```text
CANONICAL_ID_DOC = """A canonical ID of the file downloaded.

If specified and non-empty, Bazel will not take the file from cache, unless it
was added to the cache by a request with the same canonical ID.

If unspecified or empty, Bazel by default uses the URLs of the file as the
canonical ID. This helps catch the common mistake of updating the URLs without
also updating the hash, resulting in builds that succeed locally but fail on
machines without the file in the cache. This behavior can be disabled with
--repo_env={env}=0.
""".format(env = DEFAULT_CANONICAL_ID_ENV)
```

**File:** tools/build_defs/repo/cache.bzl (L39-71)
```text
def get_default_canonical_id(repository_ctx, urls):
    """Returns the default canonical id to use for downloads.

    Returns `""` (empty string) when Bazel is run with
    `--repo_env=BAZEL_HTTP_RULES_URLS_AS_DEFAULT_CANONICAL_ID=0`.

    e.g.
    ```python
    load("@bazel_tools//tools/build_defs/repo:cache.bzl", "get_default_canonical_id")
    # ...
        repository_ctx.download_and_extract(
            url = urls,
            integrity = integrity
            canonical_id = get_default_canonical_id(repository_ctx, urls),
        ),
    ```

    Args:
      repository_ctx: The repository context of the repository rule calling this utility
        function.
      urls: A list of URLs matching what is passed to `repository_ctx.download` and
        `repository_ctx.download_and_extract`.
    """
    if repository_ctx.os.environ.get(DEFAULT_CANONICAL_ID_ENV) == "0":
        return ""

    # Do not sort URLs to prevent the following scenario:
    # 1. http_archive with urls = [B, A] created.
    # 2. Successful fetch from B results in canonical ID "A B".
    # 3. Order of urls is flipped to [A, B].
    # 4. Fetch would reuse cache entry for "A B", even though A may be broken (it has never been
    #    fetched before).
    return " ".join(urls)
```
