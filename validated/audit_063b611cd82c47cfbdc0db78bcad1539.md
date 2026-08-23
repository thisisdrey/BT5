### Title
Missing revision validation on attacker-supplied `BlobIds` in `GetLFSPointers` allows unsanitized data into the `git cat-file --batch-command` protocol stream - ([File: internal/gitaly/service/blob/lfs_pointers.go])

### Summary
`GetLFSPointers` builds `gitpipe.RevisionResult{OID: git.ObjectID(blobID)}` directly from `req.GetBlobIds()` without ever calling `git.ValidateRevision`, unlike its sibling RPC `ListLFSPointers` which explicitly validates every revision before use. These unsanitized strings are fed straight into the `gitpipe.CatfileInfo` → `gitpipe.CatfileObject` pipeline, which writes them as operands into the long-lived `git cat-file --batch-command` process's request stream.

### Finding Description
`validateGetLFSPointersRequest` only checks that the repository is valid and that `BlobIds` is non-empty: [1](#0-0) 

It never calls `git.ValidateRevision`. Compare this to `ListLFSPointers`, which validates every revision string before use: [2](#0-1) 

In `GetLFSPointers`, each attacker-controlled `blobID` is wrapped unchecked into a `gitpipe.RevisionResult`: [3](#0-2) 

This is passed to `gitpipe.CatfileInfo` (an `objectInfoReader` batch-check queue) and then to `gitpipe.CatfileObject`, which calls `queue.RequestObject(ctx, it.ObjectID().Revision())` against the persistent `git cat-file --batch-command -Z --buffer` process created in `newObjectReader`: [4](#0-3) [5](#0-4) 

`git.ValidateRevision` is the only mechanism in this codebase that rejects arguments starting with `-` (git option injection), containing NUL bytes, or containing whitespace/`:`/`\` characters: [6](#0-5) 

Because `GetLFSPointers` skips this check entirely, an attacker fully controls the exact bytes that end up as the operand written to the batch-command request queue feeding `git cat-file`. The `-Z` flag switches the batch-command protocol to NUL-delimited framing precisely because path-scoped revisions may embed newlines safely, but this same design means a NUL byte embedded in the attacker string (permitted in a protobuf `string`/`bytes` field, and not blocked here since `ValidateRevision` is never invoked) can prematurely terminate one command's operand and cause the remainder of the attacker string to be reinterpreted as the start of a new batch-command line/record. This is a genuine desync surface in the underlying protocol framing that is normally prevented only by `ValidateRevision`'s NUL check — a check that is present in every other reachable caller of this queue (`ListLFSPointers`, `ListAllLFSPointers` route through the revlist/CatfileInfoAllObjects steps which only produce OIDs computed by git itself) but is missing here because `blobID` is attacker-supplied raw text rather than a git-derived OID.

### Impact Explanation
If the request queue writer does not itself append or escape the NUL delimiter independently of attacker content (unconfirmed in `internal/git/catfile/request_queue.go`, which could not be fully read), an attacker could smuggle an additional "command" into the `--batch-command` stream of an already-open `cat-file` process for their own repository. Because a repository can have `alternates`/quarantine object directories pointed at an object pool shared with forks, a successfully smuggled `contents <oid>` or `info <oid>` command could retrieve object content belonging to the pool rather than the intended repository, and that content would be returned to the attacker as an `LFSPointer` if it matches the LFS pointer heuristic. At minimum, malformed/crafted operands reaching the batch process can desynchronize the read/write protocol and crash or hang the RPC handler (DoS of the `cat-file` process/handler). This maps to GitLab's cross-repository information disclosure and DoS impact classes.

### Likelihood Explanation
The precondition is trivial: any unprivileged, authenticated caller of `GetLFSPointersRequest` needs only to set `BlobIds` to a crafted string containing a NUL byte or other control bytes — no special role, repository configuration, or additional access is required. This is directly reachable via a single gRPC call with attacker-controlled content, making it fully repeatable.

### Recommendation
Add `git.ValidateRevision([]byte(blobID))` (with NUL/whitespace/leading-dash checks enabled, and without `AllowPathScopedRevision`, since blob IDs are not path-scoped revisions) in `validateGetLFSPointersRequest`, rejecting any `blobID` that is not a syntactically valid object ID before constructing `gitpipe.RevisionResult`. Additionally, consider requiring `blobID` to match the expected OID hex length/charset for the repository's object format, which would eliminate this entire class of issues at the source.

### Proof of Concept
```go
func TestGetLFSPointers_UnvalidatedBlobID(t *testing.T) {
    // ... standard test repo setup ...
    req := &gitalypb.GetLFSPointersRequest{
        Repository: repo,
        BlobIds:    []string{"0000000000000000000000000000000000000\x00contents deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\x00"},
    }
    stream, err := client.GetLFSPointers(ctx, req)
    require.NoError(t, err)
    // Assert no ValidateRevision-style InvalidArgument error is returned,
    // and instead the request reaches the cat-file batch-command queue unsanitized,
    // unlike an equivalent crafted revision sent to ListLFSPointers which is rejected
    // with "invalid revision: revision can't contain NUL".
}
```
Compare against `ListLFSPointers` with the same crafted string in `Revisions`, which is rejected up front by `git.ValidateRevision` at [2](#0-1) , demonstrating the validation gap is specific to `GetLFSPointers`.

### Citations

**File:** internal/gitaly/service/blob/lfs_pointers.go (L38-42)
```go
	for _, revision := range in.GetRevisions() {
		if err := git.ValidateRevision([]byte(revision), git.AllowPathScopedRevision(), git.AllowPseudoRevision()); err != nil {
			return structerr.NewInvalidArgument("invalid revision: %w", err).WithMetadata("revision", revision)
		}
	}
```

**File:** internal/gitaly/service/blob/lfs_pointers.go (L157-166)
```go
	blobs := make([]gitpipe.RevisionResult, len(req.GetBlobIds()))
	for i, blobID := range req.GetBlobIds() {
		blobs[i] = gitpipe.RevisionResult{OID: git.ObjectID(blobID)}
	}

	catfileInfoIter, err := gitpipe.CatfileInfo(ctx, objectInfoReader, gitpipe.NewRevisionIterator(ctx, blobs),
		gitpipe.WithSkipCatfileInfoResult(func(objectInfo *catfile.ObjectInfo) bool {
			return objectInfo.Type != "blob" || objectInfo.Size > lfsPointerMaxSize
		}),
	)
```

**File:** internal/gitaly/service/blob/lfs_pointers.go (L183-193)
```go
func validateGetLFSPointersRequest(ctx context.Context, locator storage.Locator, req *gitalypb.GetLFSPointersRequest) error {
	if err := locator.ValidateRepository(ctx, req.GetRepository()); err != nil {
		return err
	}

	if len(req.GetBlobIds()) == 0 {
		return fmt.Errorf("empty BlobIds")
	}

	return nil
}
```

**File:** internal/git/catfile/object_reader.go (L83-118)
```go
func newObjectReader(
	ctx context.Context,
	repo gitcmd.RepositoryExecutor,
	counter *prometheus.CounterVec,
	opts ...ObjectReaderOption,
) (*objectReader, error) {
	flags := []gitcmd.Option{
		gitcmd.Flag{Name: "-Z"},
		gitcmd.Flag{Name: "--batch-command"},
		gitcmd.Flag{Name: "--buffer"},
	}

	var cfg objectReaderConfig
	for _, opt := range opts {
		opt(&cfg)
	}

	if featureflag.MailmapOptions.IsEnabled(ctx) && !cfg.disableMailmap {
		flags = append([]gitcmd.Option{gitcmd.Flag{Name: "--use-mailmap"}}, flags...)
	}

	batchCmd, err := repo.Exec(ctx,
		gitcmd.Command{
			Name:  "cat-file",
			Flags: flags,
		},
		gitcmd.WithSetupStdin(),
		gitcmd.WithSetupStdout(),
		gitcmd.WithCompletionErrorLogFilter(func(cmd *command.Command, stderr string) bool {
			return isEvictedCatfileProcessWithMigratedQuarantine(cmd.Env(), stderr)
		}),
	)
	if err != nil {
		return nil, err
	}

```

**File:** internal/git/gitpipe/catfile_object.go (L84-96)
```go
		tc, hasTrailers := it.(trailerCarrier)

		var i int64
		for it.Next() {
			if err := queue.RequestObject(ctx, it.ObjectID().Revision()); err != nil {
				sendRequest(catfileObjectRequest{err: err})
				return
			}

			var trailers []byte
			if hasTrailers {
				trailers = tc.Trailers()
			}
```

**File:** internal/git/revision.go (L70-97)
```go

	if bytes.HasPrefix(revision, []byte("-")) {
		return fmt.Errorf("revision can't start with '-'")
	}
	if bytes.Contains(revision, []byte("\x00")) {
		return fmt.Errorf("revision can't contain NUL")
	}

	if cfg.allowPathScopedRevision {
		// We don't need to validate the path component, if any, given that it may contain
		// all bytes except for the NUL byte which we already checked for above.
		revision, _, _ = bytes.Cut(revision, []byte(":"))
	}

	if !cfg.allowEmpty && len(revision) == 0 {
		return fmt.Errorf("empty revision")
	}
	if bytes.ContainsAny(revision, " \t\n\r") {
		return fmt.Errorf("revision can't contain whitespace")
	}
	if bytes.Contains(revision, []byte(":")) {
		return fmt.Errorf("revision can't contain ':'")
	}
	if bytes.Contains(revision, []byte("\\")) {
		return fmt.Errorf("revision can't contain '\\'")
	}

	return nil
```
