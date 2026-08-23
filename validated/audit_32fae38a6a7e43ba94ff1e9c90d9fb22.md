### Title
Unbounded CPU/Memory consumption in `GetFileAttributes` via unbounded `Paths`/`Attributes` cross-product - (File: internal/git/gitattributes/check_attr.go)

### Summary
`RepositoryService.GetFileAttributes` accepts attacker-controlled `Paths` and `Attributes` lists with no upper bound on size, and each path triggers a full pass over the `.gitattributes` matching engine for every requested attribute name via `check-attr --stdin -z`. Since the RPC is a unary, non-streaming request/response, an attacker can pack tens of thousands of path/attribute entries into a single request within the default gRPC message-size limit and force the Gitaly worker to do proportionally large synchronous CPU work while holding the `check-attr` subprocess open for the entire request.

### Finding Description
`validateGetFileAttributesRequest` in `internal/gitaly/service/repository/file_attributes.go` only checks that `Revision`, `Paths`, and `Attributes` are non-empty; it does not cap their length or the total size of the request: [1](#0-0) 

The handler `GetFileAttributes` then starts a single `check-attr --stdin -z <attrNames...>` process via `gitattributes.CheckAttr`, passing all requested attribute names as arguments, and loops over every path in `in.GetPaths()`, calling `checkAttrCmd.Check(path)` synchronously for each one: [2](#0-1) 

`CheckAttrCmd.Check` writes the path to the long-lived subprocess's stdin and then blocks reading `c.count` (i.e., `len(names)`) NUL-delimited triples back from stdout before returning: [3](#0-2) 

Because the call is fully synchronous, the total work done inside a single RPC invocation is `O(len(Paths) * len(Attributes) * gitattributes-matching-cost)`. An attacker who owns a repository can commit a `.gitattributes` file with many overlapping glob patterns (increasing per-path matching cost in the `check-attr` subprocess) and then call `GetFileAttributes` with a request containing tens of thousands of `Paths` entries and many `Attributes` entries. Since the RPC is unary, both `Paths` and `Attributes` must fit inside a single incoming gRPC message, but at the default gRPC max receive message size (4 MiB in `grpc-go`) this is still sufficient to fit well over 100k short path strings, which is enough to keep a `check-attr` subprocess and its owning goroutine busy for an extended, attacker-tunable duration on every call. There is no RPC-specific concurrency limit configured by default (`[[concurrency]]` limits in `internal/grpc/middleware/limithandler/middleware.go` are opt-in per-RPC and not applied to `GetFileAttributes` unless an operator explicitly configures them), so nothing in the default deployment prevents a single unprivileged owner of a repository from repeatedly issuing such requests to consume CPU on the storage node handling their repository.

### Impact Explanation
Each such request pins one goroutine and one `check-attr` git subprocess for the full duration of the cross-product computation, and the attacker can issue many such requests concurrently (bounded only by their own client concurrency, since there's no default per-RPC concurrency cap on `GetFileAttributes`). This degrades CPU availability for the storage/partition the repository lives on, matching a request-triggered resource exhaustion / DoS of an RPC handler class of finding.

### Likelihood Explanation
The precondition is minimal: the attacker only needs to own a repository (any GitLab user can create one) and be able to call `GetFileAttributes` on it with a large `Paths`/`Attributes` list, both of which are fully attacker-controlled RPC fields with no size validation. This is trivially repeatable and requires no elevated privileges, secrets, or non-default configuration.

### Recommendation
Add explicit limits in `validateGetFileAttributesRequest` (e.g., a maximum count for `Paths` and `Attributes`, and/or a maximum product of the two) and reject requests exceeding these bounds with an `InvalidArgument` error, similar to size/count limits enforced elsewhere in Gitaly RPCs. Consider also enabling a default per-RPC concurrency limit for `GetFileAttributes` in `WithConcurrencyLimiters`.

### Proof of Concept
```go
func TestGetFileAttributes_LargePathsDoS(t *testing.T) {
    ctx := testhelper.Context(t)
    cfg, client := setupRepositoryService(t)
    repoProto, repoPath := gittest.CreateRepository(t, ctx, cfg)

    // .gitattributes with many overlapping patterns increases per-path match cost.
    var sb strings.Builder
    for i := 0; i < 5000; i++ {
        fmt.Fprintf(&sb, "pattern%d/** attr%d\n", i, i%50)
    }
    gittest.WriteCommit(t, cfg, repoPath,
        gittest.WithBranch("main"),
        gittest.WithTreeEntries(
            gittest.TreeEntry{Path: ".gitattributes", Mode: "100644", Content: sb.String()},
        ))

    paths := make([]string, 100000)
    for i := range paths {
        paths[i] = fmt.Sprintf("some/deep/path%d/file.txt", i)
    }
    attrs := make([]string, 50)
    for i := range attrs {
        attrs[i] = fmt.Sprintf("attr%d", i)
    }

    start := time.Now()
    _, err := client.GetFileAttributes(ctx, &gitalypb.GetFileAttributesRequest{
        Repository: repoProto,
        Revision:   []byte("main"),
        Attributes: attrs,
        Paths:      paths,
    })
    require.NoError(t, err)
    t.Logf("elapsed: %s", time.Since(start)) // expect this to scale linearly/superlinearly with no server-side cap
}
```
Run this alongside repeated concurrent invocations to observe sustained CPU usage on the Gitaly process with no rejection from `validateGetFileAttributesRequest`.

### Citations

**File:** internal/gitaly/service/repository/file_attributes.go (L19-39)
```go
	repo := s.localRepoFactory.Build(in.GetRepository())

	checkAttrCmd, finishAttr, err := gitattributes.CheckAttr(ctx, repo, git.Revision(in.GetRevision()), in.GetAttributes())
	if err != nil {
		return nil, structerr.New("check attr: %w", err)
	}

	defer finishAttr()

	var attrValues []*gitalypb.GetFileAttributesResponse_AttributeInfo

	for _, path := range in.GetPaths() {
		attrs, err := checkAttrCmd.Check(path)
		if err != nil {
			return nil, structerr.New("check attr: %w", err)
		}

		for _, attr := range attrs {
			attrValues = append(attrValues, &gitalypb.GetFileAttributesResponse_AttributeInfo{Path: path, Attribute: attr.Name, Value: attr.State})
		}
	}
```

**File:** internal/gitaly/service/repository/file_attributes.go (L44-61)
```go
func validateGetFileAttributesRequest(ctx context.Context, locator storage.Locator, in *gitalypb.GetFileAttributesRequest) error {
	if err := locator.ValidateRepository(ctx, in.GetRepository()); err != nil {
		return err
	}

	if len(in.GetRevision()) == 0 {
		return errors.New("revision is required")
	}

	if len(in.GetPaths()) == 0 {
		return errors.New("file paths are required")
	}

	if len(in.GetAttributes()) == 0 {
		return errors.New("attributes are required")
	}

	return nil
```

**File:** internal/git/gitattributes/check_attr.go (L62-104)
```go
// Check the attributes for the file at the given path.
func (c CheckAttrCmd) Check(path string) (Attributes, error) {
	if strings.Contains(path, "\000") {
		return nil, fmt.Errorf("path with NUL byte not allowed")
	}

	c.m.Lock()
	defer c.m.Unlock()

	if _, err := c.stdin.WriteString(path + "\000"); err != nil {
		return nil, fmt.Errorf("write path: %w", err)
	}
	if err := c.stdin.Flush(); err != nil {
		return nil, fmt.Errorf("flush: %w", err)
	}

	attrs := Attributes{}
	buf := make([]string, 0, 3)

	// Using git-check-attr(1) with -z will return data in the format:
	// <path> NUL <attribute> NUL <info> NUL ...
	for i := 0; i < c.count; {
		word, err := c.stdout.ReadBytes('\000')
		if err != nil {
			return nil, fmt.Errorf("read line: %w", err)
		}

		buf = append(buf, string(bytes.TrimSuffix(word, []byte{0})))

		if len(buf) < 3 {
			continue // Keep going until we have 3 words
		}

		if buf[0] != path {
			return nil, fmt.Errorf("wrong path name detected, expected %q, got %q", path, buf[0])
		}
		if buf[2] != Unspecified {
			attrs = append(attrs, Attribute{Name: buf[1], State: buf[2]})
		}

		i++
		buf = buf[:0]
	}
```
