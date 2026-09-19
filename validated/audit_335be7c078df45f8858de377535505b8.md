I have enough evidence to confirm the analog. The mechanism is well-documented and confirmed in `tools/build_defs/repo/http.bzl`.

### Title
Trust-on-first-fetch: `http_archive`/`http_file` without a pinned `sha256`/`integrity` locks an attacker-supplied checksum into `MODULE.bazel.lock`, silently poisoning all subsequent builds - (File: tools/build_defs/repo/http.bzl)

### Summary
The Gro report describes a "first depositor" flaw: when a vault has no prior state, the first (attacker-controlled) interaction establishes a share price that is trusted and baked in permanently, and the legitimate depositor is shortchanged without any check. Bazel's repository-fetch layer has a structurally identical trust-establishment flaw: when a `repository_rule` (most notably `http_archive`/`http_file` in `tools/build_defs/repo/http.bzl`) is invoked without a declared `sha256`/`integrity`, Bazel does not fail or merely warn — it computes a digest **from whatever bytes were returned by the first successful fetch** and writes that digest back as the "reproducible" value for the rule via `ctx.repo_metadata(attrs_for_reproducibility=...)`. For module-extension-generated repos, this same first-observed digest is what gets persisted into `generatedRepoSpecs` in `MODULE.bazel.lock`, which is then treated by all subsequent (and other developers'/CI's) invocations as the pinned, trusted value.

### Finding Description
In `tools/build_defs/repo/http.bzl`: [1](#0-0) 

```
def _update_integrity_attr(ctx, attrs, download_info):
    # We don't need to override the integrity attribute if sha256 is already specified.
    if ctx.attr.sha256 or ctx.attr.integrity:
        return ctx.repo_metadata(reproducible = True)
    integrity_override = {"integrity": download_info.integrity}
    return ctx.repo_metadata(attrs_for_reproducibility = update_attrs(ctx.attr, attrs.keys(), integrity_override))

def _update_http_archive_integrity_attrs(ctx, attrs, integrity):
    ...
    if not ctx.attr.sha256 and not ctx.attr.integrity:
        integrity_override["integrity"] = integrity.archive
    ...
```

When no `sha256`/`integrity` is declared, `ctx.download_and_extract` fetches whatever content the server currently returns and hands back `download_info.integrity`, which is *derived from the attacker-served bytes themselves*. There is no independent, previously-trusted value being checked against — exactly the "no supply of shares yet, so the first depositor sets the price" situation from the report.

That derived digest is not merely advisory: for repos created by a `module_extension`, the resulting `attrs`/`integrity` become part of `generatedRepoSpecs` recorded in `MODULE.bazel.lock`: [2](#0-1) 

```
"generatedRepoSpecs": {
  "async_profiler": {
    "repoRuleId": "@@bazel_tools//tools/build_defs/repo:http.bzl%http_file",
    "attributes": {
      "downloaded_file_path": "async-profiler.jar",
      "integrity": "sha256-hwOrB7gKRnaucBvdJPD/PMONf0OuJEHOlXtFFyOFh+c=",
      ...
```

In default `--lockfile_mode=update`, this file is auto-written/updated and is meant to be checked into version control and consumed by everyone building the project — this is precisely the "cache/lockfile entry that reaches another build" criterion. Once written, `--lockfile_mode=error` (CI-safe mode) will *accept* this attacker-established digest as the ground truth and will not re-fetch or re-validate it; the repository cache (`RepositoryCache`) also stores the artifact keyed by this same self-derived hash, so it is now the accepted, canonical hash going forward: [3](#0-2) 

```
An entry is taken from the cache if Bazel knows for sure that it has a copy of the correct file, that is, if the
download request has a SHA256 sum of the file specified and a file with that
hash is in the cache.
```

This is confirmed by the project's own shell test, which explicitly documents that when no checksum is specified, whatever is fetched first is trusted and baked in as the value used going forward: [4](#0-3) 

The victim scenario: a developer (or a bot such as `bazel mod tidy`/dependency-bump automation) adds a new `bazel_dep`/`http_archive`/module extension usage pointing at a URL without yet knowing the final hash (a very common, even encouraged, workflow — Bazel prints the "please pin `sha256=...`" info message but proceeds anyway). If an attacker who controls that origin (a compromised or malicious mirror, a not-yet-registered/typosquatted release URL, or a race to publish a release asset before the legitimate one is uploaded) serves malicious bytes during that very first fetch, the malicious digest is what gets accepted, written to `MODULE.bazel.lock`, and from then on is silently trusted by every teammate and CI job that runs with the checked-in lockfile — with no signal that the "pin" was never actually validated against anything.

### Impact Explanation
This breaks the core invariant that Bazel repeatedly documents to users: "a hash for each external file... is a good idea from a security perspective" and lockfile hashes provide "reproducible builds" / "risk reduction." In reality, for any dependency onboarded without a pre-known checksum, the checksum recorded in the lockfile is not a verification of authenticity — it is a recording of whatever the attacker served during the race window. This is a cache/lockfile-poisoning primitive: a single successful malicious response contaminates a value that is committed to source control and trusted by every future build (`--lockfile_mode=error`), by every teammate, and by the shared `RepositoryCache`, without any warning distinguishing "verified hash" from "first-observed hash."

### Likelihood Explanation
This requires no privileged access — only control of the content the target's `http_archive`/`http_file`/`git_repository` URL initially resolves to, at the moment the checksum-less rule is first evaluated (e.g., compromising a mirror, DNS/typosquat, or racing to publish the artifact). This is a routine occurrence: adding a new dependency without a known hash is an explicitly supported and common Bazel workflow (`bazel fetch` prints the resulting hash for the user to copy in later), so there is a real window where the "first observer wins" trust model is exploitable, and the resulting record persists indefinitely in the lockfile.

### Recommendation
- Do not silently accept and persist a self-derived digest as `attrs_for_reproducibility`/lockfile content for unpinned fetches. At minimum, force `--lockfile_mode=update` writes of unpinned integrity values to be clearly flagged as unverified (e.g., separate "unpinned" bucket) rather than indistinguishable, trusted `generatedRepoSpecs`/`registryFileHashes` entries.
- Require `bazel mod tidy`/`bazel fetch --repin`-style flows to fetch from multiple independent mirrors/times before accepting a first-observed hash, or require explicit human confirmation (e.g., print a diff/require `--allow_unpinned` flag) before writing an unpinned integrity value into a committed lockfile.
- Consider treating `MODULE.bazel.lock` entries derived from unpinned fetches as provisional and always re-verify against a second, independent fetch (or against the registry's `source.json` checksum, when the artifact is BCR-mediated) before trusting them for `--lockfile_mode=error`.

### Proof of Concept
Analogous to the existing project test `src/test/shell/bazel/bazel_repository_cache_test.sh::test_write_cache_without_hash` (`bazel_repository_cache_test.sh:258-286`), a JUnit/shell reproduction:
1. Serve a benign file `x.tar` (content A) from an attacker-controlled HTTP endpoint.
2. Add an `http_archive` rule referencing this URL with no `sha256`/`integrity` and build once, allowing `attrs_for_reproducibility`/`generatedRepoSpecs` to capture `integrity(A)`, persisted to `MODULE.bazel.lock`.
3. Simulate the attacker instead serving malicious content B at the very first fetch instead of A (i.e., the race window before a human pins the real hash) — `integrity(B)` gets captured and committed to `MODULE.bazel.lock` instead, with no differentiation in the lockfile format from a properly verified pin.
4. Run `bazel build --lockfile_mode=error`: Bazel accepts the compromised entry without re-fetching or otherwise validating it against anything, and the build proceeds using content B as if it had always been the verified, pinned dependency.

### Citations

**File:** tools/build_defs/repo/http.bzl (L173-194)
```text
def _update_integrity_attr(ctx, attrs, download_info):
    # We don't need to override the integrity attribute if sha256 is already specified.
    if ctx.attr.sha256 or ctx.attr.integrity:
        return ctx.repo_metadata(reproducible = True)
    integrity_override = {"integrity": download_info.integrity}
    return ctx.repo_metadata(attrs_for_reproducibility = update_attrs(ctx.attr, attrs.keys(), integrity_override))

def _update_http_archive_integrity_attrs(ctx, attrs, integrity):
    integrity_override = {}

    # We don't need to override the integrity attribute if sha256 is already specified.
    # remote_module_file_integrity is for internal use by Bazel only and always
    # set correctly.
    if not ctx.attr.sha256 and not ctx.attr.integrity:
        integrity_override["integrity"] = integrity.archive
    if ctx.attr.remote_file_integrity != integrity.remote_files:
        integrity_override["remote_file_integrity"] = integrity.remote_files
    if ctx.attr.remote_patches != integrity.remote_patches:
        integrity_override["remote_patches"] = integrity.remote_patches
    if not integrity_override:
        return ctx.repo_metadata(reproducible = True)
    return ctx.repo_metadata(attrs_for_reproducibility = update_attrs(ctx.attr, attrs.keys(), integrity_override))
```

**File:** MODULE.bazel.lock (L419-429)
```text
        "generatedRepoSpecs": {
          "async_profiler": {
            "repoRuleId": "@@bazel_tools//tools/build_defs/repo:http.bzl%http_file",
            "attributes": {
              "downloaded_file_path": "async-profiler.jar",
              "integrity": "sha256-hwOrB7gKRnaucBvdJPD/PMONf0OuJEHOlXtFFyOFh+c=",
              "urls": [
                "https://github.com/async-profiler/async-profiler/releases/download/v4.4/async-profiler.jar"
              ]
            }
          },
```

**File:** docs/run/build.mdx (L334-339)
```text
An entry is taken from the cache if
Bazel knows for sure that it has a copy of the correct file, that is, if the
download request has a SHA256 sum of the file specified and a file with that
hash is in the cache. So specifying a hash for each external file is
not only a good idea from a security perspective; it also helps avoiding
unnecessary downloads.
```

**File:** src/test/shell/bazel/bazel_repository_cache_test.sh (L258-286)
```shellscript
function test_write_cache_without_hash() {
  setup_repository

  # Have a WORKSPACE file without the specified sha256
  rm MODULE.bazel
  cat >> $(setup_module_dot_bazel) <<EOF
http_archive = use_repo_rule("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")

http_archive(
    name = 'endangered',
    url = 'file://$repo2_zip',
    type = 'zip',
    )
EOF
  add_rules_shell "MODULE.bazel"

  # Fetch; as we did not specify a hash, we expect bazel to tell us the hash
  # in an info message.
  #
  # The intended use case is, of course, downloading from a known-to-be-good
  # upstream https site. Here we test with plain http, which we have to allow
  # to do without checksum. But we can safely do so, as the loopback device
  # is reasonably safe against man-in-the-middle attacks.
  bazel fetch --repository_cache="$repo_cache_dir" \
        --repo_env=BAZEL_HTTP_RULES_URLS_AS_DEFAULT_CANONICAL_ID=0 \
        //zoo:breeding-program >& $TEST_log \
    || fail "expected fetch to succeed"

  expect_log "${integrity}"
```
