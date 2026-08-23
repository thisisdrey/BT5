Confirmed: `CommitDiff` takes `MaxPatchBytesForFileExtension` directly from the client request `in.GetMaxPatchBytesForFileExtension()` [1](#0-0)  and `enforceUpperBound()` never clamps this map, only the scalar fields [2](#0-1) .

### Title
Unbounded client-supplied `MaxPatchBytesForFileExtension` bypasses diff patch-size upper bound - (File: internal/gitaly/diff/diff.go)

### Summary
The `2022-09-y2k-finance` finding is a class of bug where a hard-coded numeric assumption in a scaling/limit calculation is not applied uniformly to all inputs, causing the derived value to silently escape its intended bound. In Gitaly's diff parser, the analogous flaw is that `Limits.enforceUpperBound()` clamps every scalar diff limit (`MaxFiles`, `MaxLines`, `MaxBytes`, `SafeMax*`, `MaxPatchBytes`) to a hard-coded server-side ceiling, but it omits `MaxPatchBytesForFileExtension`, a `map[string]int` that is populated verbatim from client RPC input and is documented to **override** `MaxPatchBytes` for matching file extensions.

### Finding Description
`CommitDiff` builds `diff.Limits` from the `CommitDiffRequest` and, when `enforce_limits` is set, copies the client-supplied `max_patch_bytes_for_file_extension` map directly into `limits.MaxPatchBytesForFileExtension` with no bound checking [3](#0-2) . This `Limits` struct is then passed into `diff.NewDiffParser`, which calls `limits.enforceUpperBound()` to sanitize the request [4](#0-3) . That function clamps every scalar limit field against a fixed constant (`maxPatchBytesUpperBound = 512000`, i.e. 500KB, and similarly for bytes/lines/files) [5](#0-4) [2](#0-1) , but it never iterates or bounds `limit.MaxPatchBytesForFileExtension`.

During parsing, `maxPatchBytesForCurrentFile()` looks up the current file's extension in this unbounded map and, if present, returns that value **instead of** `MaxPatchBytes`, entirely bypassing the intended 500KB per-patch ceiling [6](#0-5) . This mirrors the PegOracle bug's root cause: a limit-normalization step applies its correction formula/bound to only part of the relevant value space, leaving another code path where the same conceptual quantity (bytes-per-patch) is computed/consumed without the safety clamp.

### Impact Explanation
An ordinary authenticated client issuing `CommitDiffRequest` with `enforce_limits=true` and a crafted `max_patch_bytes_for_file_extension` map (e.g. `{".txt": 2147483647}`) can force Gitaly to buffer and stream patch data for individual files far beyond the intended 500KB / 24MB caps that `enforceUpperBound` is meant to guarantee for all other limit fields. Since `MaxPatchBytes` exists specifically to bound per-file memory usage during diff parsing (`parser.currentDiff.Patch` accumulation in `consumeChunkLine`), this creates a resource-exhaustion vector against the `DiffService` RPC handler — memory and stream bandwidth are consumed proportionally to attacker-chosen values rather than server-enforced bounds, which is a DoS-of-handler class issue within the allowed "RPC-handler resource limits" category.

### Likelihood Explanation
The RPC field is directly reachable by any client permitted to call `CommitDiff`/`RawDiff`/`RawPatch` (unprivileged, ordinary gRPC caller — no special access needed beyond normal repo read access), and the vulnerable code path is exercised whenever `enforce_limits` is true and the extension map is non-empty, which is a normal usage pattern for GitLab's own Diff limits configuration. No race conditions or unusual timing are required, making this straightforward and reliably triggerable.

### Recommendation
In `enforceUpperBound()`, iterate `limit.MaxPatchBytesForFileExtension` and clamp every value to `maxPatchBytesUpperBound`, consistent with how `MaxPatchBytes` itself is clamped, e.g.:
```go
for ext, size := range limit.MaxPatchBytesForFileExtension {
    limit.MaxPatchBytesForFileExtension[ext] = min(size, maxPatchBytesUpperBound)
}
```

### Proof of Concept
1. Call `DiffService.CommitDiff` with `EnforceLimits=true`, `MaxPatchBytes` left at a normal value, and `MaxPatchBytesForFileExtension` set to `{".bin": math.MaxInt32}`.
2. Include a commit diff containing a large binary/text file with a `.bin` extension whose patch size exceeds 512000 bytes.
3. Observe in `maxPatchBytesForCurrentFile()` that the extension-specific limit (`math.MaxInt32`) is returned instead of the clamped `MaxPatchBytes`, so `prunePatch()`/`TooLarge` truncation in `Parse()` never triggers for that file, and the full oversized patch is buffered in `parser.currentDiff.Patch` and streamed to the client — confirming the upper-bound bypass. [7](#0-6)

### Citations

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

**File:** internal/gitaly/diff/diff.go (L104-119)
```go
const (
	// maxFilesUpperBound controls how much MaxFiles limit can reach
	maxFilesUpperBound = 5000
	// maxLinesUpperBound controls how much MaxLines limit can reach
	maxLinesUpperBound = 250000
	// maxBytesUpperBound controls how much MaxBytes limit can reach
	maxBytesUpperBound = 5000 * 5120 // 24MB
	// safeMaxFilesUpperBound controls how much SafeMaxBytes limit can reach
	safeMaxFilesUpperBound = 500
	// safeMaxLinesUpperBound controls how much SafeMaxLines limit can reach
	safeMaxLinesUpperBound = 25000
	// safeMaxBytesUpperBound controls how much SafeMaxBytes limit can reach
	safeMaxBytesUpperBound = 500 * 5120 // 2.4MB
	// maxPatchBytesUpperBound controls how much MaxPatchBytes limit can reach
	maxPatchBytesUpperBound = 512000 // 500KB
)
```

**File:** internal/gitaly/diff/diff.go (L134-146)
```go
func NewDiffParser(objectHash git.ObjectHash, src io.Reader, limits Limits) *Parser {
	limits.enforceUpperBound()

	parser := &Parser{}
	reader := bufio.NewReader(src)

	parser.objectHash = objectHash
	parser.cacheRawLines(reader)
	parser.patchReader = reader
	parser.limits = limits

	return parser
}
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

**File:** internal/gitaly/diff/diff.go (L279-287)
```go
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
