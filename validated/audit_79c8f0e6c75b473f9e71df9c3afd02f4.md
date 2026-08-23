### Title
Per-extension patch size override bypasses the hardcoded `maxPatchBytesUpperBound` resource cap - ([File: internal/gitaly/diff/diff.go])

### Summary
`CommitDiff` (and other diff RPCs backed by the shared diff parser) let an ordinary, authenticated caller supply `max_patch_bytes_for_file_extension` in `CommitDiffRequest`. This per-extension override is applied directly to bound a single file's patch size but, unlike the plain `max_patch_bytes` field, it is never clamped against the hardcoded ceiling `maxPatchBytesUpperBound` (500 KB). This mirrors the reported bug class: a hardcoded resource-limit constant that is supposed to bound execution/resource consumption is inconsistent with what is actually enforced at runtime, because an alternate, client-influenced code path escapes the cap.

### Finding Description
Gitaly defines a hard, intentional ceiling on how large a single diff patch may be: [1](#0-0) 

This ceiling is enforced for `MaxPatchBytes` via `enforceUpperBound()`: [2](#0-1) 

`enforceUpperBound()` only clamps `limit.MaxPatchBytes` (and the file/line/byte cumulative limits). It never touches the values inside `limit.MaxPatchBytesForFileExtension`, a map populated directly from client-supplied `int32` values with no upper bound applied: [3](#0-2) 

When the parser decides whether a patch is "too large," it looks up the per-extension override first and returns it unmodified if present, completely overriding the intended cap: [4](#0-3) [5](#0-4) 

Because `readNextDiff` first reads the entire patch data for the current file into `diff.Patch` in memory (there is no incremental abort while reading), and only afterward is the "too large" check performed using `maxPatchBytesForCurrentFile()`, an attacker who sets an extremely large `max_patch_bytes_for_file_extension[".ext"]` value (e.g. `2147483647`) causes:
1. The parser to buffer the entire, unbounded patch content for any file with that extension in memory before any check is even relevant.
2. The "too large" check to never trip for that extension, so the patch is never pruned via `prunePatch()`/`TooLarge`, and the full patch content is streamed back to the client in `CommitDiffResponse.RawPatchData`.

The intended defense-in-depth design (`maxPatchBytesUpperBound = 512000`, documented as controlling "how much MaxPatchBytes limit can reach") is completely undermined for any extension the caller chooses to override — exactly analogous to the reported issue where a hardcoded `MAXIMUM_GAS_LIMIT` did not actually bound the resource metric (`gasleft()`) reported/enforced at runtime.

### Impact Explanation
This is a resource-limit-of-an-RPC-handler bypass reachable by any ordinary authenticated client issuing `CommitDiff` (or other diff endpoints that build `diff.Limits` from client-controlled `MaxPatchBytesForFileExtension`). By crafting a commit with one or more very large files with a chosen extension and setting a correspondingly huge override, the client forces Gitaly to:
- buffer arbitrarily large single-file patches fully in memory (`diff.Patch` byte slice grows unbounded for that file), and
- stream that unbounded patch data back over gRPC without ever hitting the hard per-patch cap that other clients/paths are subject to.

Repeated or concurrent requests of this shape can drive excessive memory allocation and bandwidth usage on the Gitaly node, i.e. a resource-exhaustion / DoS vector for the diff-service RPC handler, defeating the safeguard that `maxPatchBytesUpperBound` was specifically designed to provide.

### Likelihood Explanation
High. No special privileges beyond normal RPC access are needed — this is a standard, documented, user-facing request field (`max_patch_bytes_for_file_extension`) on `CommitDiffRequest`. The only requirement is a commit diff containing a large file whose extension the attacker chooses to match their override key, which the caller fully controls. `TestDiffFileBeingBelowLimitForExtension` in the test suite already demonstrates that per-extension overrides can legitimately exceed the "original limit," confirming the override mechanism is designed to increase the cap with no proven upper bound of its own.

### Recommendation
Clamp every value inside `Limits.MaxPatchBytesForFileExtension` to `maxPatchBytesUpperBound` inside `enforceUpperBound()` (or when populating the map in `commit_diff.go`/other call sites), so a per-extension override can only ever be within `[0, maxPatchBytesUpperBound]`. Additionally, consider bounding the total number of bytes read into `diff.Patch` for a single file independent of `maxPatchBytesForCurrentFile()`, so memory is not fully consumed for an oversized patch before the size check can act.

### Proof of Concept
1. Create a commit that changes a large file, e.g. `big.data` (multiple megabytes of content), between `LeftCommitId` and `RightCommitId`.
2. Call `CommitDiff` with:
```
CommitDiffRequest{
  EnforceLimits: true,
  MaxFiles: 9000,
  MaxLines: 9000,
  MaxBytes: 9000,
  MaxPatchBytes: 10,
  MaxPatchBytesForFileExtension: {".data": 2147483647},
}
```
3. Observe that, unlike the `.bb`/other-extension case in `TestCommitDiff_limits` (`internal/gitaly/service/diff/commit_diff_test.go` lines 1154-1169) where a moderate override is respected, an extension override far above `maxPatchBytesUpperBound` (500000) is accepted verbatim: `diff.TooLarge` stays `false` and the entire multi-megabyte patch is streamed back in `RawPatchData`, confirming the hardcoded cap is not enforced for this code path.

### Citations

**File:** internal/gitaly/diff/diff.go (L117-118)
```go
	// maxPatchBytesUpperBound controls how much MaxPatchBytes limit can reach
	maxPatchBytesUpperBound = 512000 // 500KB
```

**File:** internal/gitaly/diff/diff.go (L203-209)
```go
	if parser.limits.EnforceLimits {
		// Apply single-file size limit
		maxPatchBytesExceeded := len(parser.currentDiff.Patch) >= parser.maxPatchBytesForCurrentFile()
		if maxPatchBytesExceeded {
			parser.prunePatch()
			parser.currentDiff.TooLarge = true
		}
```

**File:** internal/gitaly/diff/diff.go (L278-287)
```go
// enforceUpperBound ensures every limit value is within its corresponding upperbound
func (limit *Limits) enforceUpperBound() {
	limit.MaxFiles = min(limit.MaxFiles, maxFilesUpperBound)
	limit.MaxLines = min(limit.MaxLines, maxLinesUpperBound)
	limit.MaxBytes = min(limit.MaxBytes, maxBytesUpperBound)
	limit.SafeMaxFiles = min(limit.SafeMaxFiles, safeMaxFilesUpperBound)
	limit.SafeMaxLines = min(limit.SafeMaxLines, safeMaxLinesUpperBound)
	limit.SafeMaxBytes = min(limit.SafeMaxBytes, safeMaxBytesUpperBound)
	limit.MaxPatchBytes = min(limit.MaxPatchBytes, maxPatchBytesUpperBound)
}
```

**File:** internal/gitaly/diff/diff.go (L315-334)
```go
func (parser *Parser) maxPatchBytesForCurrentFile() int {
	return maxPatchBytesFor(parser.limits.MaxPatchBytes, parser.limits.MaxPatchBytesForFileExtension, parser.Diff().ToPath)
}

func maxPatchBytesFor(maxPatchBytes int, maxBytesForExtension map[string]int, toPath []byte) int {
	if len(maxBytesForExtension) > 0 {
		fileName := filepath.Base(string(toPath))
		key := filepath.Ext(fileName)

		if key == "" {
			key = fileName
		}

		if limitForExtension, ok := maxBytesForExtension[key]; ok {
			return limitForExtension
		}
	}

	return maxPatchBytes
}
```

**File:** internal/gitaly/service/diff/commit_diff.go (L120-134)
```go
	var limits diff.Limits
	if in.GetEnforceLimits() {
		limits.EnforceLimits = true
		limits.MaxFiles = int(in.GetMaxFiles())
		limits.MaxLines = int(in.GetMaxLines())
		limits.MaxBytes = int(in.GetMaxBytes())
		limits.MaxPatchBytes = int(in.GetMaxPatchBytes())

		if len(in.GetMaxPatchBytesForFileExtension()) > 0 {
			limits.MaxPatchBytesForFileExtension = map[string]int{}

			for extension, size := range in.GetMaxPatchBytesForFileExtension() {
				limits.MaxPatchBytesForFileExtension[extension] = int(size)
			}
		}
```
