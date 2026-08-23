### Title
No request size limit on GetFileAttributesRequest.Paths/Attributes enables per-request CPU/goroutine amplification via git-check-attr - (File: internal/gitaly/service/repository/file_attributes.go)

### Summary
`validateGetFileAttributesRequest` only checks that `Paths` and `Attributes` are non-empty, without any upper bound on their length, and `GetFileAttributes` then synchronously calls `checkAttrCmd.Check(path)` once per path against a single long-lived `git-check-attr --stdin -z` subprocess. Since gRPC's default max receive message size (4 MiB) is not overridden anywhere in `internal/gitaly/server/server.go`, an attacker is bounded to roughly that payload size per request, limiting (but not eliminating) the achievable amplification.

### Finding Description
`GetFileAttributes` (internal/gitaly/service/repository/file_attributes.go:14-42) builds a `CheckAttrCmd` via `gitattributes.CheckAttr` and then loops over `in.GetPaths()`, calling `checkAttrCmd.Check(path)` for every path serially [1](#0-0) . `validateGetFileAttributesRequest` only rejects empty `Paths`/`Attributes`/`Revision`, with no maximum length enforced [2](#0-1) . Each `Check` call writes the path to the subprocess's stdin and blocks reading `3 * count` NUL-delimited words from stdout, where `count = len(names)` (i.e., `len(Attributes)`) [3](#0-2) , so total work is `O(len(Paths) * len(Attributes))`. No per-request timeout, item-count cap, or streaming boundary exists in this RPC.

However, `GetFileAttributesRequest` is a plain unary message. Gitaly's gRPC server is constructed with `grpc.NewServer(serverOptions...)` in `internal/gitaly/server/server.go` without any `grpc.MaxRecvMsgSize` option, meaning it uses gRPC-Go's default limit of 4 MiB per message [4](#0-3) . That default caps the total serialized size of `Paths` + `Attributes` to ~4 MiB, which bounds — though does not eliminate — the number of entries an attacker can submit in a single call (e.g., short path/attribute strings could still yield on the order of tens of thousands of entries, and the multiplicative `len(Paths) * len(Attributes)` cost can still produce a meaningfully expensive single request). There is no additional application-level cap, no per-RPC timeout enforced by this handler, and no concurrency/rate limiter specific to `GetFileAttributes` beyond the generic `limitHandler` interceptors that are configured elsewhere (whose per-RPC concurrency configuration was not verified in this trace).

### Impact Explanation
A crafted `GetFileAttributesRequest` with many short paths and many attributes (bounded by the 4 MiB gRPC message-size ceiling) can still make a single RPC call spend significant CPU/wall time synchronously blocking on a `git-check-attr` subprocess, tying up a Gitaly worker goroutine and one OS process/pair of pipes for the call's duration. Because there is no per-call timeout enforced in the handler itself, a client that also stalls its own read after issuing such a request (or issues many such requests concurrently, subject to whatever global concurrency limiter Gitaly applies) can hold resources longer than necessary. This is a genuine, if size-bounded, DoS/resource-amplification vector on a shared multi-tenant Gitaly node.

### Likelihood Explanation
Any unprivileged GitLab user with fetch/read access to a repository (their own repo, or one they can fork/import) can invoke `GetFileAttributes`, an ACCESSOR RPC, requiring no special role. Constructing a request with the maximum permitted paths/attributes under the 4 MiB gRPC ceiling is straightforward and fully within attacker control (via gRPC directly, or by whatever GitLab Rails code path proxies to this RPC). The main limiting factor is the unmodified gRPC default message-size cap, which the attacker cannot bypass without a compromised/misconfigured server.

### Recommendation
Add explicit upper bounds in `validateGetFileAttributesRequest` on `len(in.GetPaths())` and `len(in.GetAttributes())` (e.g., reject requests exceeding a few hundred/thousand entries), and consider imposing a context deadline/timeout around the `checkAttrCmd.Check` loop so a single request cannot occupy a worker indefinitely regardless of message size.

### Proof of Concept
```go
func TestGetFileAttributes_LargeRequest(t *testing.T) {
    // Build a GetFileAttributesRequest with as many short Paths entries
    // (e.g., "a\n", "b\n", ...) and Attributes entries as fit under the
    // 4 MiB default gRPC message size, then time the RPC call and observe
    // CPU usage / goroutine count of the Gitaly process during the call.
    // Expected: no InvalidArgument is returned despite the large slice
    // sizes, and the call takes proportionally longer as
    // len(Paths) * len(Attributes) grows, demonstrating the missing
    // application-level bound in validateGetFileAttributesRequest.
}
```

### Citations

**File:** internal/gitaly/service/repository/file_attributes.go (L28-39)
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

**File:** internal/git/gitattributes/check_attr.go (L63-104)
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
```

**File:** internal/gitaly/server/server.go (L220-245)
```go
	serverOptions := []grpc.ServerOption{
		grpc.StatsHandler(tracing.NewGRPCServerStatsHandler(
			otelgrpc.WithTracerProvider(otel.GetTracerProvider()),
		)),
		grpc.StatsHandler(loghandler.PerRPCLogHandler{
			Underlying:     &grpcstats.PayloadBytes{},
			FieldProducers: []loghandler.FieldsProducer{grpcstats.FieldsProducer},
		}),
		grpc.Creds(lm),
		grpc.ChainStreamInterceptor(streamServerInterceptors...),
		grpc.ChainUnaryInterceptor(unaryServerInterceptors...),
		// We deliberately set the server MinTime to significantly less than the client interval of 20
		// seconds to allow for network jitter. We can afford to be forgiving as the maximum number of
		// concurrent clients for a Gitaly server is typically in the hundreds and this volume of
		// keepalives won't add significant load.
		grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{
			MinTime:             10 * time.Second,
			PermitWithoutStream: true,
		}),
		grpc.KeepaliveParams(keepalive.ServerParameters{
			Time: 5 * time.Minute,
		}),
		grpc.WaitForHandlers(false),
	}

	return grpc.NewServer(serverOptions...), nil
```
