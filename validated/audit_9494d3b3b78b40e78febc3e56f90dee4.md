Confirmed: `CommitDiffRequest` exposes `EnforceLimits`, `MaxFiles`/`MaxLines`/`MaxBytes`, and `CollectAllPaths` directly to any caller of the `CommitDiff` RPC [1](#0-0) , and these are passed straight into the `diff.Parser` via `diff.Limits` with no further gating.

### Title
CommitDiff resource limits are bypassed when `CollectAllPaths` is set, allowing unbounded parsing of oversized diffs - (File: internal/gitaly/diff/diff.go)

### Summary
`Parser.Parse()` is supposed to stop consuming the diff stream once `MaxFiles`/`MaxLines`/`MaxBytes` are exceeded and `EnforceLimits` is set. However, when the caller also sets `CollectAllPaths`, the code takes the "keep parsing, only clear the patch content" branch instead of terminating, and it never re-checks or shrinks the limits going forward — so the parser walks the *entire* remaining diff stream regardless of size, defeating the purpose of the size limits.

### Finding Description
In `Parser.Parse()`: [2](#0-1) 

```go
if parser.limits.EnforceLimits {
    ...
    maxLimitsExceeded := maxLinesExceeded >= 0 || maxBytesExceeded >= 0 || maxFilesExceeded > 0
    if maxLimitsExceeded && !parser.limits.PatchLimitsOnly {
        if parser.limits.CollectAllPaths {
            parser.currentDiff.CollectAllPaths = true
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

When `CollectAllPaths` is true, `parser.finished` is never set. This means `Parse()` keeps being called and keeps invoking `initializeCurrentDiff()` (which increments `filesProcessed`) and `readNextDiff()` for every subsequent file in the diff stream, for as long as the raw diff has entries — all the way to the end of the caller-controlled diff, no matter how large `MaxFiles`, `MaxLines`, or `MaxBytes` say it should be.

The `stopPatchCollection` flag only suppresses copying the raw patch text into `diff.Patch` (`consumeChunkLine`, `internal/gitaly/diff/diff.go:472-510`), but `diff.byteCount`/`diff.lineCount` are still accumulated per-line in `consumeChunkLine` [3](#0-2) , and `parser.linesProcessed`/`parser.bytesProcessed` continue to be updated every iteration [4](#0-3) . So the "limit" counters keep growing far past the configured maximum, but nothing uses that fact to stop the loop — the only gate (`parser.finished`) was deliberately skipped in the `CollectAllPaths` branch.

Because `EnforceLimits`, `MaxFiles`/`MaxLines`/`MaxBytes`, and `CollectAllPaths` are all attacker/client-controlled fields on `CommitDiffRequest` and passed through unchanged into `diff.Limits` [1](#0-0) , any ordinary Gitaly client (e.g. GitLab Rails, or anyone with RPC access) can request a `CommitDiff` between two commits with an enormous line/byte diff while setting tiny `MaxLines`/`MaxBytes`/`MaxFiles` and `CollectAllPaths: true`. The server will still fully tokenize the entire diff output line-by-line (`readNextDiff` / `consumeChunkLine`) for every file, achieving none of the CPU/memory savings the limits are meant to provide, and will do so for the full size of the underlying `git diff` output.

### Impact Explanation
This is a resource-limiting bypass in an RPC handler (`CommitDiff`), the exact category called out as acceptable ("DoS of a handler"). Limits (`MaxFiles`/`MaxLines`/`MaxBytes`) exist specifically so that clients (and by extension GitLab's diff-rendering paths, which routinely use `CollectAllPaths` to still learn about all changed paths while pruning body content) can bound Gitaly's CPU/parsing cost per request. With `CollectAllPaths` set, that bound silently stops applying: the full diff is parsed regardless of the requested caps, so a large/pathological diff (e.g. tens of thousands of files or huge line counts) drives full-cost parsing on every `CommitDiff` call, even though the caller asked Gitaly to cap it. Repeated concurrent requests against large diffs can consume disproportionate CPU on the gitaly-server process, which is the intended purpose of `MaxLines`/`MaxBytes`/`MaxFiles` and `SafeMax*` guards.

### Likelihood Explanation
Reaching this code path requires only calling the standard `CommitDiff` RPC with `EnforceLimits: true`, `CollectAllPaths: true`, and small `MaxFiles`/`MaxLines`/`MaxBytes` against two commits whose diff is very large — all fields are directly settable in `CommitDiffRequest` with no special privilege, and `CollectAllPaths` is a documented/expected feature (used by GitLab to still enumerate all paths under limits). No malicious peer, MITM, or leaked-token scenario is required — this is reachable from a normal, low-privilege gRPC caller.

### Recommendation
When `maxLimitsExceeded` is true and `CollectAllPaths` is set, still bound the amount of further parsing work: either (a) stop calling `readNextDiff` per-file body content beyond a hard ceiling independent of `CollectAllPaths` (i.e. skip fully into path-only extraction without re-scanning patch bytes byte-by-byte), or (b) enforce a secondary, non-bypassable upper limit on total files/lines/bytes scanned even in `CollectAllPaths` mode, terminating (`parser.finished = true`) once that hard ceiling is hit regardless of `CollectAllPaths`. At minimum, `filesProcessed`, `linesProcessed`, and `bytesProcessed` growing far past `MaxFiles`/`MaxLines`/`MaxBytes` should trigger termination of raw-line-level tokenization, not merely suppression of the `Patch` buffer.

### Proof of Concept
1. Create a repository with two commits whose diff is very large (e.g. thousands of files or one file with hundreds of thousands of changed lines).
2. Call `DiffService.CommitDiff` with:
   - `EnforceLimits: true`
   - `MaxFiles: 1`, `MaxLines: 1`, `MaxBytes: 1`
   - `CollectAllPaths: true`
3. Observe (e.g. via CPU/time profiling or by instrumenting `Parser.filesProcessed`) that the parser continues to call `initializeCurrentDiff()` / `readNextDiff()` for every file in the diff and fully tokenizes all patch content byte-by-byte in `consumeChunkLine`, rather than stopping after the first file/line/byte as the limits request — i.e. `parser.finished` never becomes `true` and the full stream is consumed, confirmed by reading `internal/gitaly/diff/diff.go:203-232` where the `CollectAllPaths` branch omits `parser.finished = true`.

### Citations

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

**File:** internal/gitaly/diff/diff.go (L504-507)
```go
	if updateStats {
		diff.byteCount += byteCount
		diff.lineCount++
	}
```
