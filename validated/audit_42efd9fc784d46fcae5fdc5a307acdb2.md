### Title
Off-by-one in diff file-count limit enforcement allows processing beyond `MaxFiles` cap - ([File: internal/gitaly/diff/diff.go])

### Summary
`Parser.Parse()` in `internal/gitaly/diff/diff.go` enforces three cumulative diff limits — `MaxFiles`, `MaxLines`, and `MaxBytes` — but uses an inconsistent boundary comparison for `MaxFiles` versus the other two, allowing the parser to process one extra file beyond the configured cap before enforcement disqualifies further processing. This mirrors the reported bug class where a capacity check fails to disqualify inputs that are exactly at the limit.

### Finding Description
The enforcement logic is: [1](#0-0) 

```go
maxFilesExceeded := parser.filesProcessed - parser.limits.MaxFiles
maxLinesExceeded := parser.linesProcessed - parser.limits.MaxLines
maxBytesExceeded := parser.bytesProcessed - parser.limits.MaxBytes
maxLimitsExceeded := maxLinesExceeded >= 0 || maxBytesExceeded >= 0 || maxFilesExceeded > 0
```

For `MaxLines` and `MaxBytes`, the "exceeded" condition triggers as soon as the processed count is *equal to* the configured limit (`>= 0` after subtraction). For `MaxFiles`, however, the condition only triggers when the processed count is *strictly greater than* the limit (`> 0` after subtraction). This means that when `parser.filesProcessed == parser.limits.MaxFiles` exactly, `maxFilesExceeded` evaluates to `0`, which is not `> 0`, so `maxLimitsExceeded` stays `false` purely because of the files counter — the parser is allowed to continue processing (and returning) one additional file beyond the intended cap before the limit is finally recognized on the following file. This directly parallels the reported bug class: a capacity/threshold check fails to disqualify the boundary case (`processed == cap`), silently permitting one more unit of work than intended.

The upper bounds for these limits are enforced separately via `enforceUpperBound()`: [2](#0-1) 

but that only clamps the configured limit values themselves; it does not correct the inconsistent comparison operator used when checking whether the limit has actually been reached during parsing.

### Impact Explanation
This is a resource-limit-enforcement bug in a diff-parsing code path used by Gitaly's Diff-related RPCs (`CommitDiff`, `RawDiff`, `DiffBlobs`, etc., all of which build on this shared parser). Because `EnforceLimits` is meant to cap diff processing (a defense against unbounded diff RPC handler cost/resource consumption), the off-by-one means diffs with exactly `MaxFiles` files entirely bypass the "files" trigger for the enforced cutoff, letting the parser process/return one file beyond what operators configured as the hard cap. Compared to the `MaxLines`/`MaxBytes` checks in the same statement, this is a clear discrepancy rather than intentional design, and it weakens the RPC handler's resource-limit protection by exactly one unit at the boundary — a narrow but concrete miss-enforcement of a documented safety limit.

### Likelihood Explanation
This path is reached by any unprivileged client that triggers a diff RPC producing a diff stream with a file count landing exactly on the configured `MaxFiles` boundary — a condition trivially reproducible by crafting a repository/diff request with precisely `MaxFiles` changed files. No privileged access, malicious peer, or non-production code path is required.

### Recommendation
Make the `MaxFiles` comparison consistent with `MaxLines`/`MaxBytes` by using `>= 0` (i.e., trigger enforcement when `filesProcessed == MaxFiles`, not only when it exceeds it):

```go
maxLimitsExceeded := maxLinesExceeded >= 0 || maxBytesExceeded >= 0 || maxFilesExceeded >= 0
```

### Proof of Concept
1. Configure/request diff limits with `EnforceLimits: true` and `MaxFiles: N`.
2. Produce a diff stream (e.g., via `CommitDiff`) containing exactly `N` changed files.
3. Observe that `Parser.Parse()` does not set `finished = true`/`OverflowMarker` on the `N`-th file since `maxFilesExceeded == 0` is not `> 0`; the parser proceeds to also fully process file `N+1` before the cap is finally recognized, unlike the equivalent boundary for `MaxLines`/`MaxBytes` which cuts off immediately at the exact limit.

### Citations

**File:** internal/gitaly/diff/diff.go (L211-214)
```go
		maxFilesExceeded := parser.filesProcessed - parser.limits.MaxFiles
		maxLinesExceeded := parser.linesProcessed - parser.limits.MaxLines
		maxBytesExceeded := parser.bytesProcessed - parser.limits.MaxBytes
		maxLimitsExceeded := maxLinesExceeded >= 0 || maxBytesExceeded >= 0 || maxFilesExceeded > 0
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
