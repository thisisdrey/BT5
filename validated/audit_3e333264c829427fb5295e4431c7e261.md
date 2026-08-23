## Analysis

The Sherlock finding is a classic **unbounded-loop resource-exhaustion** bug class: a public-facing entry point performs O(n) (or worse) work proportional to an ever-growing/attacker-influenced count, with no cap, so the call eventually consumes more resources than the execution environment allows and always fails/DoS's.

Searching Gitaly's RPC handlers for the same bug class, most of the "list everything" RPCs (`ListRefs`, `FindAllTags`, `ListAllCommits`, `ListLFSPointers`, `ListAllLFSPointers`) have been hardened with pagination/limit parameters and page tokens [1](#0-0) [2](#0-1) . However, `ObjectsSize` has no such protection.

### Title
Unbounded client-controlled revision stream in `ObjectsSize` allows resource-exhaustion DoS - (File: internal/gitaly/service/repository/objects_size.go)

### Summary
`RepositoryService.ObjectsSize` is a client-streaming RPC that accepts an unlimited number of stream messages, each carrying an unlimited number of revisions, and feeds every single one of them as `stdin` to a single long-lived `git rev-list --disk-usage --objects --stdin` subprocess with no cap on message count, revision count, or elapsed time/memory.

### Finding Description
The handler receives the first message, validates the repository, then spawns `git rev-list --disk-usage --objects --stdin` [3](#0-2) . It then loops forever (`for i := 0; ; i++`), reading additional client-streamed messages via `server.Recv()` until `io.EOF`, and for every revision in every message it validates it and writes it into the running rev-list process's stdin [4](#0-3) .

There is no limit on:
- the number of stream messages the client may send,
- the number of revisions per message, or
- the cumulative number of revisions fed into `git rev-list`.

This mirrors the audited bug class exactly: work performed by a single RPC invocation grows unboundedly with attacker-controlled input (here, the number of streamed revisions) rather than being capped, so the call can be driven past the resources Gitaly/the OS can supply for a single request — analogous to how `liveMarketsBy`'s cost grew unboundedly with `marketCounter`.

### Impact Explanation
An ordinary client authorized to call any accessor RPC (e.g., through the GitLab Rails/API path or direct gRPC access, same trust level as any other Gitaly RPC caller) can send an `ObjectsSize` stream containing an extremely large number of revisions across many stream messages. This will:
- Keep a `git rev-list --disk-usage --objects --stdin` subprocess and its parent goroutine alive and consuming CPU/memory for the entire duration,
- Hold a concurrency-limiter slot for that repository for an extended/indefinite period (see `doc/backpressure.md`'s per-repo concurrency queue, which only limits how many concurrent RPCs run, not how expensive one RPC can be) [5](#0-4) ,
- Potentially starve other legitimate requests to the same repository, and consume unbounded gRPC-server memory/CPU on the Gitaly node.

This is a DoS of a specific handler/repository, matching the "DoS of a handler" acceptance criterion.

### Likelihood Explanation
Likelihood is moderate-to-high: the RPC requires no special privilege beyond normal repository access that any client of `RepositoryService` has, the request shape (arbitrarily many stream messages, each with arbitrarily many revisions) is entirely attacker-controlled, and no server-side validation rejects oversized requests before doing the expensive work. gRPC per-message size limits bound a single message but not the number of messages in a client-streaming RPC, so the attack is trivially repeatable across many small messages.

### Recommendation
Add a hard cap on the total number of revisions (and/or number of stream messages / elapsed time) accepted by `ObjectsSize` per call, returning `InvalidArgument`/`ResourceExhausted` once the cap is exceeded, similar to the `limit` fields already used by `ListBlobs`, `ListAllBlobs`, `ListLFSPointers`, etc. [6](#0-5) . Alternatively, apply a context deadline/streaming byte-count guard on the growing `stdin` writer within `ObjectsSize`.

### Proof of Concept
1. Open a gRPC client-stream to `RepositoryService.ObjectsSize`.
2. Send the initial message with a valid `Repository` and a batch of revisions.
3. Continue sending thousands of additional stream messages (staying under the per-message gRPC size limit), each containing a large batch of valid revisions (e.g., all blob/commit OIDs from `git rev-list --all --objects`).
4. Observe that the single `ObjectsSize` call runs the `git rev-list --disk-usage --objects --stdin` subprocess for a very long time / with high memory usage, since [4](#0-3)  imposes no limit on the total amount of data written to the subprocess's stdin, tying up a concurrency-limiter slot and CPU/memory on the Gitaly node until the stream ends or the process is externally killed.

### Citations

**File:** internal/gitaly/service/ref/find_all_tags.go (L64-79)
```go
	// If `PageToken` is not provided, then `IsPageToken` will always return `true`
	// and disable pagination logic. If `PageToken` is set, then we will skip all tags
	// until we reach the tag equal to `PageToken`. After that, tags will be returned
	// as usual.
	pastPageToken := opts.IsPageToken([]byte{})
	limit := opts.Limit
	i := 0

	parser := catfile.NewParser()

	for catfileObjectsIter.Next() {
		tag := catfileObjectsIter.Result()

		if i >= limit {
			break
		}
```

**File:** internal/gitaly/service/commit/list_all_commits.go (L69-77)
```go
	limit := request.GetPaginationParams().GetLimit()
	parser := catfile.NewParser()

	for i := int32(0); catfileObjectIter.Next(); i++ {
		// If we hit the pagination limit, then we stop sending commits even if there are
		// more commits in the pipeline.
		if limit > 0 && limit <= i {
			break
		}
```

**File:** internal/gitaly/service/repository/objects_size.go (L38-53)
```go
	var stderr, stdout strings.Builder
	cmd, err := repo.Exec(ctx,
		gitcmd.Command{
			Name: "rev-list",
			Flags: []gitcmd.Option{
				gitcmd.Flag{Name: "--disk-usage"},
				gitcmd.Flag{Name: "--objects"},
				gitcmd.Flag{Name: "--stdin"},
			},
		},
		gitcmd.WithStderr(&stderr),
		gitcmd.WithStdout(&stdout),
		gitcmd.WithSetupStdin())
	if err != nil {
		return fmt.Errorf("start rev-list command: %w", err)
	}
```

**File:** internal/gitaly/service/repository/objects_size.go (L55-84)
```go
	for i := 0; ; i++ {
		if i != 0 && request.GetRepository() != nil {
			return structerr.NewInvalidArgument("subsequent requests must not contain repository")
		}

		if len(request.GetRevisions()) == 0 {
			return structerr.NewInvalidArgument("no revisions specified")
		}

		for _, revision := range request.GetRevisions() {
			if err := git.ValidateRevision(revision, git.AllowPseudoRevision()); err != nil {
				return structerr.NewInvalidArgument("validating revision: %w", err).WithMetadata("revision", revision)
			}

			// Each revision must be separated by a newline when the `--stdin` option is used, as Git
			// parses these differently to command-line arguments.
			if _, err := cmd.Write([]byte(fmt.Sprintf("%s\n", revision))); err != nil {
				return structerr.NewInvalidArgument("process revision: %w", err).WithMetadata("revision", revision)
			}
		}

		request, err = server.Recv()
		if err != nil {
			if errors.Is(err, io.EOF) {
				break
			}

			return fmt.Errorf("receiving next request: %w", err)
		}
	}
```

**File:** doc/backpressure.md (L17-24)
```markdown
Limit the number of concurrent RPCs that are in flight on each Gitaly node for each
repository per RPC using `[[concurrency]]` configuration:

```toml
[[concurrency]]
rpc = "/gitaly.SmartHTTPService/PostUploadPackWithSidechannel"
max_per_repo = 1
```
```

**File:** proto/blob.proto (L160-163)
```text
  repeated string revisions = 2;
  // limit is the maximum number of blobs to return. If set to its default
  // (`0`), then all found blobs will be returned.
  uint32 limit = 3;
```
