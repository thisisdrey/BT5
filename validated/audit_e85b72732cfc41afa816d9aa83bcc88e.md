### Title
`Trading._handleOpenFees`-style accounting bug analog: `diff.Parser.prunePatch` under-tracks `bytesProcessed`, letting crafted diffs bypass the `MaxBytes` hard limit in `CommitDiff` - (File: `internal/gitaly/diff/diff.go`)

### Summary
`CommitDiff` lets a caller request `EnforceLimits=true` with a `MaxBytes` hard cap on the total diff size Gitaly will parse/buffer for a commit comparison [1](#0-0) . Enforcement relies on `Parser.bytesProcessed`, a running counter that is supposed to accurately reflect how many diff bytes have been accumulated so far [2](#0-1) . When a per-file patch is pruned (either because the "safe" collapse threshold or the hard per-file `MaxPatchBytes` threshold is hit), `prunePatch()` decrements this same counter using a different, larger byte count than what was originally added to it, causing `bytesProcessed` to be under-reported. This lets a crafted commit diff process (and buffer in memory) far more bytes than the `MaxBytes` hard limit is supposed to allow, defeating the very DoS protection the limit exists for.

### Finding Description
Just like the Tigris `_handleOpenFees` bug — where an amount was subtracted from a running "fees paid" total using a formula that excluded a component (`referralFees`) that had never been added to that total in the first place, silently inflating `_positionSize` beyond what should have been allowed — Gitaly's diff parser has the same class of mismatched increment/decrement on a resource-limit counter.

- When a chunk line is a hunk header (`@@ ... @@`), `consumeChunkLine` is called with `updateStats=false`, so the header bytes are appended to `diff.Patch` but are **not** counted into `diff.byteCount` [3](#0-2) [4](#0-3) .
- After a diff is read, only `diff.byteCount` (which excludes hunk-header bytes) is added to the parser's cumulative `bytesProcessed` counter [2](#0-1) .
- When the patch is later pruned — either via the `CollapseDiffs`/safe-limit path or via the hard per-file `MaxPatchBytes` path — `prunePatch()` subtracts `len(parser.currentDiff.Patch)` from `bytesProcessed`, which **includes** the hunk-header bytes that were never added in the first place [5](#0-4) [6](#0-5) .

Because the subtraction always removes more than was added (the discrepancy equals the total hunk-header byte length for that file), each pruned file drives the tracked `bytesProcessed` counter artificially low — potentially net-negative relative to before the file was processed. The subsequent hard-limit check `maxBytesExceeded := parser.bytesProcessed - parser.limits.MaxBytes` (line 213) is therefore evaluated against an undercounted value, exactly mirroring how Tigris's `_positionSize` was computed from an undercounted `_feePaid`.

### Impact Explanation
This is a resource-limit bypass of an RPC handler (`CommitDiff`) similar in class to the referenced report's fee/limit miscalculation. A client (any user able to invoke `CommitDiff` with `EnforceLimits`/`MaxBytes` set, or GitLab Rails constructing such requests on the user's behalf for a merge-request diff view) can push a commit containing many files whose diffs are individually large enough to be collapsed/pruned but contain outsized hunk-header context lines. Each such file leaks (rather than consumes) budget from the enforced `MaxBytes` accounting, allowing the parser to continue accepting and buffering additional diff content well beyond what the caller configured as a hard cap, on a repository the attacker fully controls the content of. This directly undermines the DoS protection that `EnforceLimits`/`MaxBytes` is meant to provide (analogous to a limiter permitting more "capacity" — memory/CPU — than intended), and can be used to force Gitaly to buffer and process oversized diffs despite the caller asking for strict limits.

### Likelihood Explanation
Reachable purely through the public `CommitDiff` RPC with attacker-controlled repository content (commits/trees the user pushed) and standard request parameters (`EnforceLimits`, `CollapseDiffs`/`SafeMaxBytes`, `MaxBytes`, `MaxPatchBytes`) — no privileged access, leaked token, or malicious peer/MITM condition is required. The only uncertainty is the magnitude of the discrepancy achievable per file (bounded by how much hunk-header/context text git includes per hunk, which is influenced but not fully controlled by the pushed file content) and whether it is large enough in practice to matter versus the configured `MaxBytes`; this would require empirical measurement to fully confirm severity, but the underlying accounting mismatch is unambiguous from the code.

### Recommendation
Make `prunePatch()` subtract exactly what was previously added to `bytesProcessed`/`linesProcessed` for the current diff, rather than re-deriving the subtraction from `len(Patch)` (which can include bytes such as hunk headers that were never included in the added `byteCount`). Concretely, track and subtract `parser.currentDiff.byteCount` (the same field that was added at line 190) instead of `len(parser.currentDiff.Patch)`:

```go
func (parser *Parser) prunePatch() {
	parser.linesProcessed -= parser.currentDiff.lineCount
	parser.bytesProcessed -= parser.currentDiff.byteCount // use the value that was actually added
	parser.currentDiff.ClearPatch()
}
```

### Proof of Concept
1. Construct a repository commit where a modified file's diff contains many hunks, each with a nontrivial trailing "context" string in the `@@ ... @@` header (git includes surrounding function/context text in the hunk header when it can be determined), while keeping the actual changed lines small.
2. Call `CommitDiff` with `EnforceLimits=true`, `CollapseDiffs=true` (or set `MaxPatchBytes` low enough to trigger the hard per-file prune), and a small `MaxBytes`.
3. Because each pruned file subtracts `len(Patch)` (content + hunk headers) from `bytesProcessed` while only `byteCount` (content only) was ever added, `bytesProcessed` drops further than it rose for that file.
4. Repeat across many files in the same commit: the cumulative `bytesProcessed` counter used to compare against `MaxBytes` (line 213) stays artificially low, so the `maxBytesExceeded` condition is never (or much later) triggered, allowing the parser to keep processing/buffering diff content well past the configured `MaxBytes` hard limit — verifiable by observing the total bytes actually returned/buffered by the RPC exceeding `MaxBytes` despite `EnforceLimits=true`.

### Citations

**File:** internal/gitaly/service/diff/commit_diff.go (L120-126)
```go
	var limits diff.Limits
	if in.GetEnforceLimits() {
		limits.EnforceLimits = true
		limits.MaxFiles = int(in.GetMaxFiles())
		limits.MaxLines = int(in.GetMaxLines())
		limits.MaxBytes = int(in.GetMaxBytes())
		limits.MaxPatchBytes = int(in.GetMaxPatchBytes())
```

**File:** internal/gitaly/diff/diff.go (L188-190)
```go
	// Update parser line and byte counts.
	parser.linesProcessed += parser.currentDiff.lineCount
	parser.bytesProcessed += parser.currentDiff.byteCount
```

**File:** internal/gitaly/diff/diff.go (L195-209)
```go
	if parser.limits.CollapseDiffs && parser.isOverSafeLimits() && parser.currentDiff.lineCount > 0 {
		parser.prunePatch()
		parser.currentDiff.Collapsed = true
		if parser.limits.CollectAllPaths {
			parser.currentDiff.CollectAllPaths = true
		}
	}

	if parser.limits.EnforceLimits {
		// Apply single-file size limit
		maxPatchBytesExceeded := len(parser.currentDiff.Patch) >= parser.maxPatchBytesForCurrentFile()
		if maxPatchBytesExceeded {
			parser.prunePatch()
			parser.currentDiff.TooLarge = true
		}
```

**File:** internal/gitaly/diff/diff.go (L256-260)
```go
		case bytes.HasPrefix(line, []byte("@@")):
			// Hunk headers do not count towards limits.
			if err := consumeChunkLine(reader, diff, skipPatch, false); err != nil {
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
