### Title
Unbounded single-file diff patch accumulation before size-limit enforcement enables memory-exhaustion DoS via `CommitDiff` - (File: internal/gitaly/diff/diff.go)

### Summary
`diff.Parser.Parse()` fully buffers each file's diff hunk into `Diff.Patch` before any size limit is checked. Because the accumulation loop that reads a single patch (`readNextDiff` → `consumeChunkLine`) has no per-line or per-patch byte cap while it runs, an attacker who can get Gitaly to diff a commit containing a pathological single-file change (e.g. one extremely long unbroken line) can force Gitaly to allocate an arbitrarily large in-memory buffer before `MaxPatchBytes`/`MaxBytes`/`MaxLines` are ever evaluated — mirroring the reported Ethos bug where a safety check is applied only *after* the resource has already been consumed, based on a value that no longer reflects the real risk.

### Finding Description
`consumeChunkLine` reads a hunk line with `bufio.Reader.ReadSlice('\n')` in a loop, and on `bufio.ErrBufferFull` it simply "keeps reading," appending every chunk to `diff.Patch` with no upper bound: [1](#0-0) 

This function is called from `readNextDiff`, which walks the entire patch for the *current file* to completion before returning: [2](#0-1) 

Only once `readNextDiff` has returned — i.e., only after the complete patch for that file has already been buffered in memory — does `Parse()` check the enforced limits: [3](#0-2) 

The single-file "hard" cap (`maxPatchBytesForCurrentFile()`) and the cumulative `MaxBytes`/`MaxLines`/`MaxFiles` checks are computed from `len(parser.currentDiff.Patch)` and the processed counters — values that only exist *after* the entire patch content has already been read into memory. There is no bound applied while the bytes are being accumulated, and `enforceUpperBound()` only clamps the limit values themselves (e.g. `maxPatchBytesUpperBound = 512000`), not the actual amount of data Gitaly buffers per diff hunk during a single call to `readNextDiff`/`consumeChunkLine`.

This is reachable from an ordinary user's push: any user who can push a commit (fork, MR branch, etc.) controls the exact byte content of files in that commit, including creating a single file with one enormous line with no newlines. When any consumer (e.g. GitLab Rails via `CommitDiff`) requests a diff for that commit, `eachDiff()` spawns `git diff --patch --raw` and feeds its stdout into `diff.NewDiffParser`: [4](#0-3) [5](#0-4) 

### Impact Explanation
Because the buffering happens before enforcement, a crafted commit with one file containing a very large unbroken line (or an extremely large single hunk) forces Gitaly to allocate memory proportional to attacker-controlled file content for that one diff invocation, regardless of the caller-specified `MaxPatchBytes`/`MaxBytes` values. Repeated or concurrent `CommitDiff` calls against such commits can be used to drive up Gitaly's memory usage, resulting in a denial-of-service condition against the RPC handler (and potentially the whole gitaly-daemon under memory pressure), even though the caller explicitly requested small `MaxPatchBytes`/`MaxBytes` limits intended to bound resource consumption.

### Likelihood Explanation
Any authenticated user capable of pushing a commit (including into a personal fork or feature branch which is diffed automatically by GitLab, e.g. via merge-request diff rendering) can trigger this: they need only craft one file with a single very long line (a common, unremarkable content shape — e.g. minified assets, base64 blobs, generated code) to defeat the size-based safety limits before they are checked. No special privileges, hooks bypass, or timing race is required, making this a fairly reliably reproducible resource-exhaustion path.

### Recommendation
Bound patch accumulation while it is happening rather than only after the fact:
- In `consumeChunkLine`/`readNextDiff`, stop appending to `diff.Patch` (or abort the read) once accumulated bytes for the current file exceed `maxPatchBytesForCurrentFile()` (or a hard absolute ceiling such as `maxPatchBytesUpperBound`), instead of only checking `len(parser.currentDiff.Patch) >= maxPatchBytesForCurrentFile()` after the whole patch has already been read.
- Similarly, enforce `MaxBytes`/`MaxLines` incrementally within `readNextDiff` so that files exceeding these limits stop being buffered mid-read rather than only being flagged in `Parse()` afterward.
- Ensure any single unbroken line read via `ReadSlice`/`bufio.ErrBufferFull` looping has an absolute cap independent of `skipPatch`/limit configuration, since `git diff` output for adversarial content can produce one line far larger than any configured patch limit.

### Proof of Concept
1. As a user with push access to a repository (e.g., a fork), create a commit containing a file (e.g. `evil.txt`) whose content is a single line with no `\n` and is many megabytes/gigabytes long (git will treat this as one diff hunk/line).
2. Trigger a `CommitDiff` RPC (as GitLab Rails does when rendering a commit/MR diff) with `EnforceLimits=true` and small `MaxPatchBytes`/`MaxBytes` values, per `internal/gitaly/service/diff/commit_diff.go` (`limits.MaxPatchBytes = int(in.GetMaxPatchBytes())`, etc.).
3. Observe that Gitaly's `git diff` subprocess output for that one file is fully buffered into `Diff.Patch` inside `consumeChunkLine`/`readNextDiff` (`internal/gitaly/diff/diff.go:472-510`, `internal/gitaly/diff/diff.go:234-276`) before the `maxPatchBytesExceeded`/`maxBytesExceeded` checks in `Parse()` (`internal/gitaly/diff/diff.go:203-229`) ever run — memory usage for that single request scales with the attacker-chosen line size, not with the configured limits. [6](#0-5)

### Citations

**File:** internal/gitaly/diff/diff.go (L183-229)
```go
	if err := readNextDiff(parser.patchReader, &parser.currentDiff, parser.stopPatchCollection); err != nil {
		parser.err = err
		return false
	}

	// Update parser line and byte counts.
	parser.linesProcessed += parser.currentDiff.lineCount
	parser.bytesProcessed += parser.currentDiff.byteCount

	// PatchSize is needed for clients to determine if patch exceeded the soft or hard limit when patch was pruned.
	parser.currentDiff.PatchSize = int32(len(parser.currentDiff.Patch))

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

**File:** internal/gitaly/diff/diff.go (L234-276)
```go
func readNextDiff(reader *bufio.Reader, diff *Diff, skipPatch bool) error {
	for currentPatchDone := false; !currentPatchDone || reader.Buffered() > 0; {
		// We cannot use bufio.Scanner because the line may be very long.
		line, err := reader.Peek(10)
		if errors.Is(err, io.EOF) {
			// If the last diff has an empty patch (e.g. --ignore-space-change),
			// patchReader will read EOF, but Parser not finished.
			currentPatchDone = true
		} else if err != nil {
			return fmt.Errorf("peek diff line: %w", err)
		}

		switch {
		case bytes.HasPrefix(line, []byte("diff --git")):
			// If the next diff header is detected, the current patch is complete.
			return nil
		case helper.ByteSliceHasAnyPrefix(line, "---", "+++") && len(diff.Patch) == 0:
			// File headers occur before the first hunk header and therefore before any patch data
			// has been recorded.
			if err := discardLine(reader); err != nil {
				return err
			}
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
		default:
			if err := discardLine(reader); err != nil {
				return err
			}
		}
	}

	return nil
}
```

**File:** internal/gitaly/diff/diff.go (L472-510)
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
}
```

**File:** internal/gitaly/service/diff/utils.go (L36-61)
```go
func (s *server) eachDiff(ctx context.Context, repo *localrepo.Repo, objectHash git.ObjectHash, subCmd gitcmd.Command, limits diff.Limits, callback func(*diff.Diff) error) error {
	diffConfig := gitcmd.ConfigPair{Key: "diff.noprefix", Value: "false"}

	cmd, err := repo.Exec(ctx, subCmd, gitcmd.WithConfig(diffConfig), gitcmd.WithSetupStdout())
	if err != nil {
		return structerr.NewInternal("cmd: %w", err)
	}

	diffParser := diff.NewDiffParser(objectHash, cmd, limits)

	for diffParser.Parse() {
		if err := callback(diffParser.Diff()); err != nil {
			return err
		}
	}

	if err := diffParser.Err(); err != nil {
		return structerr.NewInternal("parse failure: %w", err)
	}

	if err := cmd.Wait(); err != nil {
		return structerr.NewFailedPrecondition("%w", err)
	}

	return nil
}
```

**File:** internal/gitaly/service/diff/commit_diff.go (L120-142)
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

	if err := s.eachDiff(ctx, repo, objectHash, cmd, limits, func(diff *diff.Diff) error {
```
