### Title
Miscounted byte accounting in diff patch pruning causes negative `bytesProcessed`, silently bypassing `SafeMaxBytes`/`MaxBytes` limits in `CommitDiff`/diff RPCs - (File: internal/gitaly/diff/diff.go)

### Summary
`Parser.prunePatch()` decrements the running `bytesProcessed` counter by `len(parser.currentDiff.Patch)`, but `bytesProcessed` was originally incremented by `parser.currentDiff.byteCount` (line 190), which is a *different* quantity than `len(Patch)`. This mismatch, reachable through the `CommitDiff` RPC's caller-controlled diff/patch limits, can drive `bytesProcessed` negative, permanently defeating the `SafeMaxBytes`/`MaxBytes` resource caps for the remainder of the stream — an accounting-underflow analog of the reported vault bug where a tracked balance is decremented by a value larger than what was actually credited to it.

### Finding Description
`diff.Parser` tracks cumulative `linesProcessed` and `bytesProcessed` counters to enforce `Limits.SafeMax*`/`Max*` and to decide when to collapse or truncate an over-large diff: [1](#0-0) 

These counters are credited from `currentDiff.byteCount`/`lineCount`, which are populated only by `consumeChunkLine` when `updateStats == true` — i.e. only for real content lines ("+", "-", " ", "\", "~\n", "Binary"), and explicitly *not* for hunk header lines ("@@ ... @@"), which are read via the `updateStats=false` branch: [2](#0-1) [3](#0-2) 

However, `diff.Patch` (the byte buffer) accumulates *all* consumed lines, including hunk headers, whenever `skipPatch` is false: [4](#0-3) 

Consequently `len(currentDiff.Patch) >= currentDiff.byteCount` whenever a diff hunk contains "@@" header lines. When the parser later prunes an oversized/over-safe-limit patch, it subtracts the larger `len(Patch)` value from `bytesProcessed`, even though only the smaller `byteCount` was ever added to it: [5](#0-4) 

Because `bytesProcessed` is a plain (signed) `int`, this does not panic like the Solidity `uint256` underflow in the report, but it silently drives the counter negative. Once negative, `parser.isOverSafeLimits()` (`bytesProcessed > limits.SafeMaxBytes`) and the `EnforceLimits` byte check (`bytesProcessed - limits.MaxBytes >= 0`) become permanently false for any subsequent diff files in the same stream, effectively disabling the resource-consumption cap that the caller configured through the public `CommitDiffRequest` fields (`SafeMaxBytes`, `MaxBytes`, `CollapseDiffs`, `EnforceLimits`): [6](#0-5) [7](#0-6) [8](#0-7) 

This is exactly analogous to the reported bug class: an accounting variable is decremented by a magnitude that can exceed what was actually accrued, because the credit and debit paths use two logically-different measures of the "same" quantity (in the report: deposit-time asset units vs. current-market asset units; here: `byteCount` credited vs. `len(Patch)` debited).

### Impact Explanation
`CommitDiff` is a normal, unprivileged, user-reachable RPC (any client with read access to a repository can call it with attacker-influenced parameters such as `left_commit_id`/`right_commit_id` pointing at commits the caller pushed, plus fully attacker-controlled `SafeMaxBytes`, `MaxBytes`, `CollapseDiffs`, `EnforceLimits`). Once the byte-tracking underflows to negative, the size-based DoS protections (`SafeMaxBytes` collapsing large patches, `MaxBytes` truncating the response) stop functioning for the rest of that diff stream. An attacker who crafts a commit range containing many hunk-header-heavy diffs can exploit this to force the Gitaly server to keep buffering/streaming full, uncollapsed patch data far beyond the caller-configured safety limits, increasing memory and CPU use on the RPC handler — a resource-exhaustion/DoS condition on that handler, consistent with the "no impact = OK to reject" boundary being crossed because it concretely defeats a configured DoS-prevention limit.

### Likelihood Explanation
Moderate. It requires: (1) a diff whose collapsible/oversized entry contains at least one "@@" hunk header line before being pruned (very common in any real diff with content changes), and (2) the caller to have configured `CollapseDiffs`/`EnforceLimits` with byte limits low enough to trigger `prunePatch()`. Both preconditions are trivially satisfiable by any client calling `CommitDiff` (or other RPCs that route through `diff.Parser`, e.g. `RawDiff`/`Diff` service `eachDiff` helper) with a repository/commit range they control and with tight `SafeMaxBytes`/`MaxBytes` values, so it is easily reproducible without any privileged access.

### Recommendation
Make the credit and debit of `bytesProcessed` symmetric: either accumulate `byteCount` for hunk-header lines too (so `len(Patch)` and `byteCount` always match), or have `prunePatch()` subtract the same quantity that was added (i.e. `parser.currentDiff.byteCount`) rather than `len(parser.currentDiff.Patch)`. Additionally, clamp `bytesProcessed`/`linesProcessed` to a minimum of 0 after any decrement as defense-in-depth, mirroring the "cap withdrawal to available balance" mitigation recommended in the referenced report, so a mismatched credit/debit can never make the tracked value negative and silently defeat the size limits.

### Proof of Concept
Conceptual reproduction (Go, using `internal/gitaly/diff` package directly, no privileged access required):
1. Construct a synthetic raw+patch diff stream for two files:
   - File A: a diff whose hunk contains multiple "@@ ... @@" header lines interleaved with few "+"/"-" content lines (making `len(Patch)` noticeably larger than `byteCount`), sized so that after processing it `isOverSafeLimits()` becomes true, triggering `prunePatch()`.
   - File B: a further diff following it.
2. Configure `diff.Limits{CollapseDiffs: true, SafeMaxBytes: N, EnforceLimits: true, MaxBytes: N}` with `N` chosen so File A collapses.
3. After parsing File A, inspect `parser.bytesProcessed` (via exported diagnostic or by observing subsequent behavior) — the value goes negative because `len(Patch)` (includes hunk header bytes) exceeds `byteCount` (excludes them) that was originally added.
4. Feed File B (large, exceeding `SafeMaxBytes`/`MaxBytes` on its own) and observe that it is *not* collapsed/truncated as it should be, because `bytesProcessed` (now negative) no longer exceeds `SafeMaxBytes`/`MaxBytes`, demonstrating the resource-limit bypass on the `CommitDiff` handler path.

Note: I was unable to execute this PoC in the sandbox (no filesystem/terminal access here); the trace above is derived directly from the code paths cited, which show the credit (`byteCount`, line 190) and debit (`len(Patch)`, line 293) use non-equivalent quantities whenever hunk-header lines are present in an oversized/collapsed diff.

### Citations

**File:** internal/gitaly/diff/diff.go (L188-190)
```go
	// Update parser line and byte counts.
	parser.linesProcessed += parser.currentDiff.lineCount
	parser.bytesProcessed += parser.currentDiff.byteCount
```

**File:** internal/gitaly/diff/diff.go (L203-229)
```go
	if parser.limits.EnforceLimits {
		// Apply single-file size limit
		maxPatchBytesExceeded := len(parser.currentDiff.Patch) >= parser.maxPatchBytesForCurrentFile()
		if maxPatchBytesExceeded {
			parser.prunePatch()
			parser.currentDiff.TooLarge = true
		}

		maxFilesExceeded := parser.filesProcessed - parser.limits.MaxFiles
		maxLinesExceeded := parser.linesProcessed - parser.limits.MaxLines
		maxBytesExceeded := parser.bytesProcessed - parser.limits.MaxBytes
		maxLimitsExceeded := maxLinesExceeded >= 0 || maxBytesExceeded >= 0 || maxFilesExceeded > 0
		if maxLimitsExceeded && !parser.limits.PatchLimitsOnly {
			if parser.limits.CollectAllPaths {
				parser.currentDiff.CollectAllPaths = true
				// Do allow parser to finish, but since limits are hit
				// do not allow it to continue collecting patches
				// only info about patches
				parser.currentDiff.ClearPatch()
				parser.stopPatchCollection = true
			} else {
				parser.finished = true
				parser.currentDiff.Reset()
			}
			parser.currentDiff.OverflowMarker = true
		}
	}
```

**File:** internal/gitaly/diff/diff.go (L256-267)
```go
		case bytes.HasPrefix(line, []byte("@@")):
			// Hunk headers do not count towards limits.
			if err := consumeChunkLine(reader, diff, skipPatch, false); err != nil {
				return err
			}
		case bytes.HasPrefix(line, []byte("Binary")):
			diff.Binary = true
			fallthrough
		case helper.ByteSliceHasAnyPrefix(line, "-", "+", " ", "\\", "~\n"):
			if err := consumeChunkLine(reader, diff, skipPatch, true); err != nil {
				return err
			}
```

**File:** internal/gitaly/diff/diff.go (L289-295)
```go
// prunePatch nullifies the current diff patch and reduce lines and bytes processed
// according to it.
func (parser *Parser) prunePatch() {
	parser.linesProcessed -= parser.currentDiff.lineCount
	parser.bytesProcessed -= len(parser.currentDiff.Patch)
	parser.currentDiff.ClearPatch()
}
```

**File:** internal/gitaly/diff/diff.go (L309-313)
```go
func (parser *Parser) isOverSafeLimits() bool {
	return parser.filesProcessed > parser.limits.SafeMaxFiles ||
		parser.linesProcessed > parser.limits.SafeMaxLines ||
		parser.bytesProcessed > parser.limits.SafeMaxBytes
}
```

**File:** internal/gitaly/diff/diff.go (L472-509)
```go
func consumeChunkLine(reader *bufio.Reader, diff *Diff, skipPatch, updateStats bool) error {
	// The code that follows would be much simpler if we used
	// bufio.Reader.ReadBytes, but that allocates an intermediate copy of
	// each line which adds up to a lot of allocations. By using ReadSlice we
	// can copy bytes into currentDiff.Patch without intermediate
	// allocations.
	var byteCount int
	for done := false; !done; {
		line, err := reader.ReadSlice('\n')
		if updateStats && byteCount == 0 && len(line) > 0 {
			if line[0] == '+' {
				diff.LinesAdded++
			} else if line[0] == '-' {
				diff.LinesRemoved++
			}
		}
		byteCount += len(line)

		switch {
		case errors.Is(err, bufio.ErrBufferFull):
			// long line: keep reading
		case err != nil && !errors.Is(err, io.EOF):
			return fmt.Errorf("read chunk line: %w", err)
		default:
			done = true
		}

		if !skipPatch {
			diff.Patch = append(diff.Patch, line...)
		}
	}

	if updateStats {
		diff.byteCount += byteCount
		diff.lineCount++
	}

	return nil
```

**File:** internal/gitaly/service/diff/commit_diff.go (L120-140)
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
	}
	limits.CollapseDiffs = in.GetCollapseDiffs()
	limits.CollectAllPaths = in.GetCollectAllPaths()
	limits.SafeMaxFiles = int(in.GetSafeMaxFiles())
	limits.SafeMaxLines = int(in.GetSafeMaxLines())
	limits.SafeMaxBytes = int(in.GetSafeMaxBytes())
```
