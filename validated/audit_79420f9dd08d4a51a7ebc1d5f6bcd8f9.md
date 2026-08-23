### Title
Unbounded in-memory buffering of attacker-controlled commit content in `FindLocalBranches` - ([File: internal/gitaly/service/ref/util.go])

### Summary
The `commitIterator.Next()` used by `FindLocalBranches` reads `git-for-each-ref` output that includes the full `%(contents)` (commit body) field and accumulates it into an ever-growing `[]byte` slice until a `\x00\n` record delimiter is found, before any size limit is applied. Since a commit body can be made arbitrarily large by any user with push access, this mirrors the ERC165Checker bug class: the code assumes a response of bounded size but the "callee" (here, the repository content driven entirely by the requester) can force unbounded resource consumption in the caller.

### Finding Description
`NewBranchIterator` spawns `git for-each-ref --format=...%(contents)...` and hands its stdout to a `commitIterator`, whose `Next()` method repeatedly reads into a small (`4096`-byte) buffer and appends everything read to `c.accumulated` until it can locate the `\x00\n` record delimiter: [1](#0-0) 

Only once the delimiter is found and the full record has been split out is `buildBranch` invoked, which truncates the commit body field to `helper.MaxCommitOrTagMessageSize`: [2](#0-1) 

The truncation happens too late — it operates on data that has already been fully materialized in `c.accumulated`. There is no cap on how large `c.accumulated` may grow while waiting for the delimiter, so a single ref pointing at a commit with an arbitrarily large commit message (the `%(contents)` field has no upper bound enforced by Git or by Gitaly at read time) will cause the entire message to be buffered in memory before it is ever truncated. This is directly analogous to the ERC165Checker issue: the caller (`commitIterator`) assumes the "response" (one ref's formatted record) is small and bounded, but the data source (a commit body fully controlled by whoever authored the commit) can return an unbounded amount of data, and the caller has no gas/memory limit gating consumption before processing.

This code path is reachable via the unprivileged, ordinary `FindLocalBranches` RPC: [3](#0-2) 
which calls `findRefsWithIterator` → `NewBranchIterator` → `commitIterator.Next()`.

An ordinary user can create such a condition simply by pushing a commit with a very large commit message and making a branch point at it (a completely normal, permitted git operation), then any caller of `FindLocalBranches` against that repository (e.g., GitLab's own branch-listing UI/API, or any client with read access) triggers unbounded buffering.

### Impact Explanation
Each concurrent `FindLocalBranches` call against a repository containing a branch with a huge commit message can force Gitaly to allocate memory proportional to the size of that attacker-supplied commit message before any truncation occurs. Because commit message size is not bounded prior to accumulation, a small number of concurrent requests against a repository with one or more such branches can exhaust available memory on the Gitaly node, causing a denial of service for that node (and, depending on replication topology, an entire shard). This matches the "DoS of a handler" acceptance criterion.

### Likelihood Explanation
Likelihood is high: creating a commit with an arbitrarily large message and pushing it is trivial and requires no elevated privileges — a standard `git commit` with a large `-m` message (or committing with a message body populated via a hook-less local commit before push) is enough. `FindLocalBranches` is a common, frequently-invoked accessor RPC (used for branch listings), making the trigger path easy to reach without any special conditions.

### Recommendation
Enforce a maximum accumulation size in `commitIterator.Next()` (e.g., bound `c.accumulated` growth to `helper.MaxCommitOrTagMessageSize` plus a small delimiter margin) and fail/truncate the read for a given record once that bound is exceeded, rather than truncating only after the full record has already been read into memory. Alternatively, avoid requesting `%(contents)` unbounded from `for-each-ref` and instead request a size-limited format specifier (e.g., `%(contents:size=N)` if supported) or perform truncation via `git`'s own formatting rather than in Go after full materialization.

### Proof of Concept
1. Create a repository and commit with a commit message body far larger than `helper.MaxCommitOrTagMessageSize` (e.g., tens of megabytes), then create a local branch pointing to it — both are ordinary, permitted git operations for any user with write access.
2. Invoke the `FindLocalBranches` RPC (or `FindAllBranches`/any RPC that flows through `NewBranchIterator`) against that repository.
3. Observe that `commitIterator.Next()` reads the entire multi-megabyte commit body into `c.accumulated` before `buildBranch` truncates it, causing memory usage proportional to the attacker-chosen commit message size for the duration of the request; repeating the call concurrently multiplies memory pressure on the Gitaly node. [4](#0-3)

### Citations

**File:** internal/gitaly/service/ref/util.go (L96-103)
```go
			committer.Name = element
		case 8:
			if len(element) > helper.MaxCommitOrTagMessageSize {
				element = element[:helper.MaxCommitOrTagMessageSize]
			}

			commit.Body = element
			commit.BodySize = int64(len(element))
```

**File:** internal/gitaly/service/ref/util.go (L218-298)
```go
type commitIterator struct {
	reader         *bufio.Reader
	err            error
	currentBranch  *gitalypb.Branch
	stderr         bytes.Buffer
	numLines       int
	foundPageToken bool
	opts           *findRefsOpts
	done           bool
	cmd            *command.Command
	lineDelimiter  []byte
	accumulated    []byte
	buffer         []byte
}

var fullCommitFields = []string{
	"%(refname)",
	"%(objectname)",
	"%(authorname)",
	"%(subject)",
	"%(authoremail)",
	"%(authordate:unix)",
	"%(authordate:format:%z)",
	"%(committername)",
	"%(contents)",
	"%(committeremail)",
	"%(committerdate:unix)",
	"%(committerdate:format:%z)",
	"%(contents:signature)",
	"%(tree)",
	"%(parent)",
}

// NewBranchIterator creates a new iterator that populates branch information
func NewBranchIterator(
	ctx context.Context,
	repo gitcmd.RepositoryExecutor,
	opts *findRefsOpts,
	patterns []string,
) (Iterator, error) {
	// An extra character is necessary for the delimiter between lines
	// because there might be \n characters in the commit body.
	c := &commitIterator{
		stderr:         bytes.Buffer{},
		opts:           opts,
		foundPageToken: !opts.PageTokenError,
		lineDelimiter:  []byte("\x00\n"),
		accumulated:    []byte{},
		buffer:         make([]byte, 4096),
	}

	options := []gitcmd.Option{
		// %00 inserts the null character into the output (see for-each-ref docs)
		gitcmd.Flag{Name: "--format=" + strings.Join(fullCommitFields, "%00") + "%00"},
	}

	if opts.sortBy != "" {
		options = append(options, gitcmd.Flag{Name: "--sort=" + opts.sortBy})
	}

	for _, pattern := range opts.excludePatterns {
		options = append(options, gitcmd.Flag{Name: "--exclude=" + pattern})
	}

	cmd, err := repo.Exec(ctx, gitcmd.Command{
		Name:  "for-each-ref",
		Flags: options,
		Args:  patterns,
	}, gitcmd.WithSetupStdout(), gitcmd.WithStderr(&c.stderr))
	if err != nil {
		return nil, fmt.Errorf("spawning for-each-ref: %w", err)
	}

	c.cmd = cmd

	reader := bufio.NewReader(cmd)

	c.reader = reader

	return c, nil
}
```

**File:** internal/gitaly/service/ref/util.go (L311-324)
```go
	for {
		n, err := c.reader.Read(c.buffer)
		if n > 0 {
			c.accumulated = append(c.accumulated, c.buffer[:n]...)
		}

		if err != nil && err != io.EOF {
			c.err = err
			return false
		}

		if len(c.accumulated) == 0 {
			return false
		}
```

**File:** internal/gitaly/service/ref/find_local_branches.go (L1-1)
```go
package ref
```
