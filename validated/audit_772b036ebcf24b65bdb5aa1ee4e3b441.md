### Title
Unbounded `GetFileAttributes` request paths/attributes drive unbounded synchronous `git-check-attr(1)` CPU/memory work - ([File: internal/gitaly/service/repository/file_attributes.go])

### Summary
`validateGetFileAttributesRequest` only checks that `Paths` and `Attributes` are non-empty, with no upper bound on their length or content, and `GetFileAttributes` synchronously iterates `checkAttrCmd.Check(path)` for every path against a single long-lived `git-check-attr --stdin` subprocess. Combined with an attacker-controlled `.gitattributes` in a repo the attacker owns, this allows an unprivileged user to trigger a request that keeps a Gitaly worker goroutine and the `git-check-attr` subprocess busy for an extended, unbounded amount of CPU/wall time, with no per-request size cap or context-cancellation check inside the `Check` loop.

### Finding Description
`validateGetFileAttributesRequest` in `internal/gitaly/service/repository/file_attributes.go` validates only presence, not size, of `in.GetPaths()` and `in.GetAttributes()`: [1](#0-0) 

`GetFileAttributes` then loops synchronously over every path, calling `checkAttrCmd.Check(path)` for each one against a single shared `git-check-attr --stdin -z` subprocess, with no batching, pagination, or check of `ctx.Done()` inside the loop: [2](#0-1) 

`CheckAttrCmd.Check` itself performs a blocking write to the subprocess and a blocking read loop that has no cancellation/timeout handling — it will simply keep reading until it has consumed `c.count` triples of NUL-delimited output or errors out: [3](#0-2) 

Because the attacker owns the repository, they fully control the `.gitattributes` content (number/complexity of pattern rules) that `git-check-attr` must evaluate against each requested path, and they fully control the number of paths sent in a single `GetFileAttributesRequest`. Neither dimension is bounded by Gitaly: there is no cap on `len(in.GetPaths())`, no cap on `len(in.GetAttributes())`, and no cap on `.gitattributes` complexity (that file is ordinary repository content, not validated at all). The only implicit ceiling is the default gRPC max receive message size (4 MiB, unless the server config raises it), which still permits tens of thousands of short path/attribute strings per request, and an attacker can issue many such requests concurrently/sequentially since there is no per-RPC concurrency limiter specifically applied to `GetFileAttributes` in this codebase.

### Impact Explanation
This maps to a DoS-of-RPC-handler / resource-exhaustion class impact: a single `GetFileAttributesRequest` can force Gitaly to spend a large, attacker-tunable amount of CPU/wall-clock time inside a synchronous per-path loop invoking `git-check-attr(1)`, whose per-call cost scales with both the number of paths and the complexity/size of `.gitattributes` in the target repo. Repeated or concurrent calls of this kind can consume proportionally more CPU cores and file descriptors/subprocess slots on the Gitaly node, degrading service for other tenants/repos hosted on the same node, though it does not grant cross-repository access, credential leakage, or command injection.

### Likelihood Explanation
The precondition is that the attacker owns (or forks/imports) a repository, which is available to any unprivileged GitLab user, and that some client path (e.g. a Web IDE / Workspaces integration) exposes `GetFileAttributes` and lets the user influence the paths list and/or repository content. Both preconditions are attacker-controllable without any special role, and the attack is trivially repeatable (no rate limiting or per-RPC concurrency cap specific to this RPC was found in the codebase).

### Recommendation
- Bound `len(in.GetPaths())` and `len(in.GetAttributes())` in `validateGetFileAttributesRequest` to a sane maximum (matching similar RPCs' path/limit conventions), rejecting oversized requests with `InvalidArgument`.
- Check `ctx.Done()`/`ctx.Err()` inside the `Check` loop in `GetFileAttributes` (and ideally inside `CheckAttrCmd.Check`) so that a canceled or timed-out request stops promptly rather than running to completion.
- Consider applying a per-RPC concurrency/rate limit for `GetFileAttributes` via the existing `limithandler` middleware, consistent with other potentially expensive RPCs.

### Proof of Concept
```go
func TestGetFileAttributes_UnboundedPathsAndAttributes(t *testing.T) {
    // Setup: create a repo with a .gitattributes file containing thousands of
    // glob patterns, e.g.:
    //   for i := 0; i < 5000; i++ {
    //       fmt.Fprintf(&sb, "**/**/**/dir%d/*.ext%d filter=lfs diff=lfs merge=lfs -text\n", i, i)
    //   }
    // Commit this .gitattributes at HEAD.

    // Build a GetFileAttributesRequest with tens of thousands of Paths, e.g.:
    //   paths := make([]string, 50000)
    //   for i := range paths { paths[i] = fmt.Sprintf("some/deep/path/file%d.ext", i) }
    //
    //   req := &gitalypb.GetFileAttributesRequest{
    //       Repository: repo,
    //       Revision:   []byte("HEAD"),
    //       Paths:      paths,
    //       Attributes: []string{"filter", "diff", "merge", "text"},
    //   }
    //
    // Call client.GetFileAttributes(ctx, req) and measure wall-clock and
    // subprocess CPU (via /proc/<pid>/stat for the git-check-attr child)
    // during the call — expect this to scale roughly linearly (or worse)
    // with len(Paths) * complexity(.gitattributes), with no request-level
    // rejection from validateGetFileAttributesRequest, and no early
    // termination if the client context is canceled.
}
```

### Citations

**File:** internal/gitaly/service/repository/file_attributes.go (L28-42)
```go
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

	return &gitalypb.GetFileAttributesResponse{AttributeInfos: attrValues}, nil
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

**File:** internal/git/gitattributes/check_attr.go (L63-106)
```go
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

	return attrs, nil
```
