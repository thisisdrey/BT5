### Title
Count-based (not byte-size-based) response batching in `FindAllBranches`/`FindLocalBranches`/`FindAllRemoteBranches` allows oversized in-memory gRPC responses from attacker-controlled commit data - (File: internal/gitaly/service/ref/util.go)

### Summary
Gitaly's own `chunk.Chunker` streaming helper enforces a per-message byte budget (`proto.Size(it)` checked against `maxMessageSize` before appending) precisely to avoid the "materialize-then-check-size" pattern described in the report. However, the `FindAllBranches`, `FindLocalBranches`, and `FindAllRemoteBranches` RPCs do not use `chunk.Chunker`; they use the older `internal/helper/lines` package, which batches by a fixed **item count** (`ItemsPerMessage = 20`) rather than by serialized byte size, and only calls `stream.Send()` after the full batch of parsed `Branch`/`GitCommit` messages has been built in memory.

### Finding Description
`chunk.Chunker.Send()` proactively measures the marshaled size of every item via `proto.Size(it)` and flushes before the running total would exceed `maxMessageSize` (1 MiB): [1](#0-0) 

By contrast, `internal/helper/lines.writer.addLine()` flushes purely based on a count threshold (`ItemsPerMessage`, default 20), with no notion of the actual byte size of the accumulated lines or of what the caller will build from them: [2](#0-1) 

`FindLocalBranches`, `FindAllBranches`, and `FindAllRemoteBranches` all use this `lines.Sender` path. Their writer callbacks receive up to `ItemsPerMessage` raw ref lines, resolve each one into a full `*gitalypb.Branch` (which embeds a `*gitalypb.GitCommit`, including subject/body/trailers) via `buildBranchWithCatfile`/`buildAllBranchesBranch`, accumulate all of them into a Go slice, and only then call `stream.Send()` on the fully-built response — with no check of the resulting message's serialized size at any point: [3](#0-2) 

`findRefs()` wires this `lines.Sender` into `for-each-ref` output processing with no additional size guard: [4](#0-3) 

Because a Git commit message body is attacker-controllable and has no hard size cap enforced by Gitaly at write time, a user with ordinary push access can create branches pointing at commits with very large commit messages/trailers. When `FindAllBranches`/`FindLocalBranches`/`FindAllRemoteBranches` is later invoked (an unprivileged, read-only ACCESSOR RPC available to any caller with read access to the repository), the handler will unconditionally batch 20 such branches — each carrying an oversized `GitCommit` — into one in-memory `Branch` slice before attempting to send it, exactly mirroring the reported bug class: the size-limiting mechanism (item count, standing in for the "response size cap") is checked/derived independently of the actual payload size that gets materialized, so the oversized message is only discovered at send time (post-construction), not before.

### Impact Explanation
An oversized outbound gRPC message causes the underlying `stream.Send()` to fail (gRPC enforces a message-size ceiling), but by that point the full slice of `ItemsPerMessage` fully-parsed `GitCommit` objects (each potentially containing megabytes of attacker-supplied commit-message data) has already been allocated in Gitaly's memory. Because `ItemsPerMessage` is a fixed count rather than a byte budget, this pattern gives no proportional relationship between configured limits and actual memory/response size, unlike `chunk.Chunker`-based RPCs. Repeated or concurrent invocation of these RPCs against repositories with crafted commits can drive elevated memory pressure and repeated request failures, degrading availability of the `RefService` for that repository (DoS of a handler).

### Likelihood Explanation
Moderate. It requires an actor with ordinary push access to create a small number of commits with abnormally large commit messages/trailers (Git does not impose a message-size limit, and Gitaly does not appear to enforce one either), then have any caller invoke `FindAllBranches`, `FindLocalBranches`, or `FindAllRemoteBranches` — all of which are unauthenticated-adjacent, side-effect-free ACCESSOR RPCs reachable by anyone with read access to the repo (including automated GitLab Rails calls that are triggered on ordinary repo browsing).

### Recommendation
Replace the count-based `lines.ItemsPerMessage` batching used by `findLocalBranches`, `newFindAllBranchesWriter`, and `newFindAllRemoteBranchesWriter` with the byte-size-aware `chunk.Chunker` (as is already done for `ListLFSPointers`, `ListBlobs`, etc.), so that a batch is flushed based on `proto.Size()` of the built `Branch`/`GitCommit` messages rather than a fixed item count. Alternatively, add an explicit size check on the constructed response before calling `stream.Send()` and split/truncate oversized commit message fields before they are placed into a `GitCommit`.

### Proof of Concept
1. As a user with push access, create a branch whose tip commit has a commit message body of several megabytes (e.g., `git commit --allow-empty -F huge_message.txt` with a multi-MB message, then push).
2. Repeat for ~20 branches (or interleave with normal branches) so that a single `ItemsPerMessage`-sized batch (20 refs) processed by `findRefs`/`lines.Send` includes multiple such oversized commits.
3. Call `FindAllBranches` (or `FindLocalBranches`/`FindAllRemoteBranches`) against the repository.
4. Observe that `newFindAllBranchesWriter`'s callback fully materializes all 20 `Branch` messages (each embedding the oversized `GitCommit`) into `branches` before calling `stream.Send()`, at which point the aggregate message may exceed the gRPC message-size limit and fail — after the full in-memory allocation has already occurred, evidencing the same "materialize-then-discover-oversize" pattern as the reported JSON-RPC issue. [5](#0-4)

### Citations

**File:** internal/helper/chunk/chunker.go (L30-51)
```go
// maxMessageSize is maximum size per protobuf message
const maxMessageSize = 1 * 1024 * 1024

// Send will append an item to the current chunk and send the chunk if it is full.
func (c *Chunker) Send(it proto.Message) error {
	if c.size == 0 {
		c.s.Reset()
	}

	itSize := proto.Size(it)

	if itSize+c.size >= maxMessageSize {
		if err := c.sendResponseMsg(); err != nil {
			return err
		}
		c.s.Reset()
	}

	c.s.Append(it)
	c.size += itSize

	return nil
```

**File:** internal/helper/lines/send.go (L35-93)
```go
var (
	// ItemsPerMessage establishes the threshold to flush the buffer when using the
	// `Send` function. It's a variable instead of a constant to make it possible to
	// override in tests.
	ItemsPerMessage = 20

	// ErrInvalidPageToken represents an error when the provided page token is invalid
	ErrInvalidPageToken = errors.New("could not find page token")
)

// Sender handles a buffer of lines from a Git command
type Sender func([][]byte, bool) error

type writer struct {
	sender  Sender
	lines   [][]byte
	options SenderOpts
}

// CopyAndAppend adds a newly allocated copy of `e` to the `s` slice. Useful to
// avoid io buffer shennanigans
func CopyAndAppend(s [][]byte, e []byte) [][]byte {
	line := make([]byte, len(e))
	copy(line, e)
	return append(s, line)
}

// flush calls the `sender` handler function with the accumulated lines and
// clears the lines buffer.
func (w *writer) flush(hasNextPage bool) error {
	if len(w.lines) == 0 { // No message to send, just return
		return nil
	}

	if err := w.sender(w.lines, hasNextPage); err != nil {
		return err
	}

	// Reset the message
	w.lines = nil

	return nil
}

// addLine adds a new line to the writer buffer. If the buffer is at capacity,
// it flushes the existing lines first before appending the new line. This
// ensures the last line always remains in the buffer for the final flush in
// consume(), which has the correct hasNextPage value.
func (w *writer) addLine(p []byte) error {
	if len(w.lines) >= ItemsPerMessage {
		if err := w.flush(false); err != nil {
			return err
		}
	}

	w.lines = CopyAndAppend(w.lines, p)

	return nil
}
```

**File:** internal/gitaly/service/ref/util.go (L134-200)
```go
func newFindLocalBranchesWriter(stream gitalypb.RefService_FindLocalBranchesServer, objectReader catfile.ObjectContentReader) lines.Sender {
	return func(refs [][]byte, hasNextPage bool) error {
		ctx := stream.Context()
		var response *gitalypb.FindLocalBranchesResponse

		var branches []*gitalypb.Branch

		for _, ref := range refs {
			elements, err := parseRef(ref, len(localBranchFormatFields))
			if err != nil {
				return err
			}

			branch, err := buildBranchWithCatfile(ctx, objectReader, elements)
			if err != nil {
				return err
			}

			branches = append(branches, branch)
		}

		response = &gitalypb.FindLocalBranchesResponse{LocalBranches: branches}

		return stream.Send(response)
	}
}

func newFindAllBranchesWriter(stream gitalypb.RefService_FindAllBranchesServer, objectReader catfile.ObjectContentReader) lines.Sender {
	return func(refs [][]byte, hasNextPage bool) error {
		var branches []*gitalypb.FindAllBranchesResponse_Branch
		ctx := stream.Context()

		for _, ref := range refs {
			elements, err := parseRef(ref, len(localBranchFormatFields))
			if err != nil {
				return err
			}
			branch, err := buildAllBranchesBranch(ctx, objectReader, elements)
			if err != nil {
				return err
			}
			branches = append(branches, branch)
		}
		return stream.Send(&gitalypb.FindAllBranchesResponse{Branches: branches})
	}
}

func newFindAllRemoteBranchesWriter(stream gitalypb.RefService_FindAllRemoteBranchesServer, objectReader catfile.ObjectContentReader) lines.Sender {
	return func(refs [][]byte, hasNextPage bool) error {
		var branches []*gitalypb.Branch
		ctx := stream.Context()

		for _, ref := range refs {
			elements, err := parseRef(ref, len(localBranchFormatFields))
			if err != nil {
				return err
			}
			branch, err := buildBranchWithCatfile(ctx, objectReader, elements)
			if err != nil {
				return err
			}
			branches = append(branches, branch)
		}

		return stream.Send(&gitalypb.FindAllRemoteBranchesResponse{Branches: branches})
	}
}
```

**File:** internal/gitaly/service/ref/util.go (L429-459)
```go
func (s *server) findRefs(ctx context.Context, writer lines.Sender, repo gitcmd.RepositoryExecutor, patterns []string, opts *findRefsOpts) error {
	var options []gitcmd.Option

	if len(opts.cmdArgs) == 0 {
		options = append(options, gitcmd.Flag{Name: "--format=%(refname)"}) // Default format
	} else {
		options = append(options, opts.cmdArgs...)
	}

	for _, pattern := range opts.excludePatterns {
		options = append(options, gitcmd.Flag{Name: "--exclude=" + pattern})
	}

	var stderr strings.Builder
	cmd, err := repo.Exec(ctx, gitcmd.Command{
		Name:  "for-each-ref",
		Flags: options,
		Args:  patterns,
	}, gitcmd.WithSetupStdout(), gitcmd.WithStderr(&stderr))
	if err != nil {
		return fmt.Errorf("spawning for-each-ref: %w", err)
	}

	if err := lines.Send(cmd, writer, lines.SenderOpts{
		IsPageToken:    opts.IsPageToken,
		Delimiter:      opts.delim,
		Limit:          opts.Limit,
		PageTokenError: opts.PageTokenError,
	}); err != nil {
		return fmt.Errorf("sending lines: %w", err)
	}
```
