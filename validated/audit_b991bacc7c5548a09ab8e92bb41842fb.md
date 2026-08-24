### Title
Unbounded `Paths`/`Attributes` in `GetFileAttributesRequest` causes synchronous, unbounded per-RPC CPU and memory consumption - ([File: internal/gitaly/service/repository/file_attributes.go])

### Summary
`GetFileAttributes` iterates over every entry in the attacker-controlled `in.GetPaths()` repeated field and, for each one, performs a blocking `WriteString`/`Flush`/`ReadBytes` round trip against a single long-lived `git-check-attr` process, accumulating all results in an in-memory `attrValues` slice before returning a single unary response. Neither `validateGetFileAttributesRequest` nor `CheckAttrCmd.Check` impose any cap on the number of paths, the number of attributes, or the total size of the accumulated response.

### Finding Description
`GetFileAttributes` in `internal/gitaly/service/repository/file_attributes.go` only validates that `Paths` and `Attributes` are non-empty: [1](#0-0) 

There is no `MaxPaths`/`MaxAttributes` check. The handler then loops synchronously over every path: [2](#0-1) 

Each iteration calls `CheckAttrCmd.Check`, which does a blocking write to stdin, a flush, and then reads `c.count` NUL-delimited tokens from the `git-check-attr` process's stdout — one full round trip per path, per attribute set: [3](#0-2) 

Because `git-check-attr` is a single long-lived process guarded by `c.m.Mutex`, this work is strictly serialized: N paths × M attribute checks always require N synchronous request/response exchanges with one external process, with no timeout on that exchange other than the overall RPC context, and no cap on the resulting slice size held in memory before the response is returned. The request size is not unbounded, however — since none of the gRPC server construction in `internal/gitaly/server/server.go` or `internal/gitaly/server/server_factory.go` explicitly configures `grpc.MaxRecvMsgSize`, the default grpc-go unary message size ceiling (4 MiB) implicitly bounds the number of `Paths`/`Attributes` strings that can be delivered in one request. This still permits on the order of ~100K short path strings per request while requiring no privileged access — only read access to a repository and any valid revision.

### Impact Explanation
An unprivileged user with read access to any repository (including their own fork/import) can send a single `GetFileAttributesRequest` with a large `Paths` list (bounded only by the ~4 MiB default gRPC message size) and multiple `Attributes`, forcing the Gitaly worker to perform tens/hundreds of thousands of synchronous read/write round trips to a single `git-check-attr` subprocess and to build a correspondingly large in-memory response before replying. This ties up one RPC-handling goroutine and the backing `git-check-attr` process for an extended, attacker-influenced duration, and increases per-RPC memory usage proportional to attacker input, degrading availability for the RPC handler / worker. This falls in the DoS-of-RPC-handler impact class, but is bounded (not literally OOM-unbounded) because of the implicit default gRPC message-size limit.

### Likelihood Explanation
Reaching this code path requires no privilege beyond ordinary repository read access: the caller supplies `Repository`, `Revision`, `Paths`, and `Attributes`, all attacker-controlled fields with no server-side upper bound in `validateGetFileAttributesRequest`. The attack is trivially repeatable and requires only a single crafted gRPC call, making it straightforward to reproduce, though its severity is capped by the default gRPC 4 MiB unary receive-size limit rather than truly unbounded growth.

### Recommendation
Add an explicit, low upper bound on `len(in.GetPaths())` and `len(in.GetAttributes())` in `validateGetFileAttributesRequest` (e.g., a few thousand paths/attributes), and/or process paths in bounded batches with periodic context/deadline checks so a single RPC cannot monopolize a `git-check-attr` process indefinitely. Additionally, consider explicitly setting `grpc.MaxRecvMsgSize` to a documented value rather than relying on the grpc-go library default.

### Proof of Concept
```go
func TestGetFileAttributes_LargePathsList(t *testing.T) {
    ctx := testhelper.Context(t)
    cfg, repoProto, _ := setupRepositoryService(t)
    client := newRepositoryClient(t, cfg, cfg.SocketPath)

    paths := make([]string, 150000)
    for i := range paths {
        paths[i] = fmt.Sprintf("path/to/file-%d.txt", i)
    }

    start := time.Now()
    resp, err := client.GetFileAttributes(ctx, &gitalypb.GetFileAttributesRequest{
        Repository: repoProto,
        Revision:   []byte("HEAD"),
        Paths:      paths,
        Attributes: []string{"eol", "text", "diff"},
    })
    elapsed := time.Since(start)
    require.NoError(t, err)
    // Assert: RPC latency/memory grows linearly with attacker-supplied Paths length,
    // with no server-side cap rejecting the oversized request.
    t.Logf("elapsed=%s, attrInfos=%d", elapsed, len(resp.GetAttributeInfos()))
}
```
This demonstrates the absence of a `MaxPaths`/`MaxAttributes` validation check in `validateGetFileAttributesRequest`, causing RPC latency and per-request memory to scale directly with the attacker-supplied `Paths` count, up to the implicit ~4 MiB default gRPC message-size ceiling.

### Citations

**File:** internal/gitaly/service/repository/file_attributes.go (L28-41)
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
