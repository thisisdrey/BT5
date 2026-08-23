### Title
Unbounded `GetFileAttributesRequest` attribute/path lists enable CPU/memory exhaustion in `CheckAttrCmd.Check` - (File: internal/git/gitattributes/check_attr.go)

### Summary
`validateGetFileAttributesRequest` never bounds `len(in.GetAttributes())` or `len(in.GetPaths())`, so an unprivileged caller can submit a `GetFileAttributesRequest` with tens of thousands of attribute names and paths. This drives `git check-attr --stdin -z` with a huge argument list and forces the handler to loop `len(paths) * len(attributes)` times reading NUL-delimited tokens, causing significant CPU/memory consumption in a single RPC handler goroutine.

### Finding Description
`GetFileAttributes` in `internal/gitaly/service/repository/file_attributes.go` only checks that `in.GetPaths()` and `in.GetAttributes()` are non-empty, with no upper bound: [1](#0-0) 

It then calls `gitattributes.CheckAttr`, which spawns `git check-attr --stdin -z --source <rev> <name1> ... <nameN>` with `Args: names` set directly from `in.GetAttributes()`: [2](#0-1) 

For every entry in `in.GetPaths()`, `GetFileAttributes` calls `checkAttrCmd.Check(path)`, which writes the path to the process's stdin and then reads exactly `c.count` (i.e. `len(names)`) NUL-delimited 3-tuples from stdout: [3](#0-2) [4](#0-3) 

The net cost of the loop is `O(len(paths) * len(attributes))` reads/appends, entirely driven by attacker-controlled request field lengths. There is no cap in `internal/gitaly/service/repository/file_attributes.go`, and I found no evidence of a custom gRPC `MaxRecvMsgSize` override in the repo (searches for `MaxRecvMsgSize`/`GRPC_MAX`/`MaxMsgSize` returned no results), so the standard ~4 MB gRPC message-size default applies — which still permits tens of thousands of short strings in both repeated fields, e.g. 10,000 attribute names and 10,000 paths comfortably fit under 4 MB while producing 10^8 loop iterations.

### Impact Explanation
This is a resource-exhaustion issue: a single crafted `GetFileAttributesRequest` can consume disproportionate CPU/memory in a Gitaly worker (large `git check-attr` argv plus a large synchronous read loop), degrading or blocking that goroutine and the underlying git subprocess for the duration of the call. It maps to the "denial of service of an RPC handler" bounty class. It is not a data-exfiltration, RCE, or cross-repo access issue — impact is limited to availability/performance degradation of the Gitaly node processing the request.

### Likelihood Explanation
The attacker only needs the ability to invoke the `GetFileAttributes` RPC (via any GitLab feature or direct API/gRPC access) against a repository they can reach, and to control the `attributes` and `paths` fields — no push of a malicious `.gitattributes` file, no elevated privileges, and no non-default configuration are required. This makes the issue directly and repeatably triggerable by any caller of the RPC.

### Recommendation
Add explicit upper bounds on `len(in.GetAttributes())` and `len(in.GetPaths())` in `validateGetFileAttributesRequest` (returning `InvalidArgument` when exceeded), matching bounds used elsewhere in Gitaly for repeated-field RPCs, and consider capping total combined attribute-name argv length passed to `git check-attr`.

### Proof of Concept
```go
// Pseudocode gRPC test against RepositoryService.GetFileAttributes
attrs := make([]string, 20000)
for i := range attrs {
    attrs[i] = fmt.Sprintf("a%d", i)
}
paths := make([]string, 20000)
for i := range paths {
    paths[i] = fmt.Sprintf("p%d", i)
}

req := &gitalypb.GetFileAttributesRequest{
    Repository: repo,
    Revision:   []byte("HEAD"),
    Attributes: attrs,
    Paths:      paths,
}
// req serializes to well under the 4MB default gRPC message limit.
// Handler executes git check-attr with 20000 argv entries and then
// performs ~20000*20000 = 4*10^8 ReadBytes('\000') loop iterations,
// consuming significant CPU/memory for the single request; measure
// wall-clock latency/CPU to confirm no cap is applied.
resp, err := client.GetFileAttributes(ctx, req)
```
Expected (current) behavior: the handler processes the full cross-product with no size validation, demonstrating the missing bound in `validateGetFileAttributesRequest`.

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

**File:** internal/git/gitattributes/check_attr.go (L30-60)
```go
func CheckAttr(ctx context.Context, repo gitcmd.RepositoryExecutor, revision git.Revision, names []string) (*CheckAttrCmd, func(), error) {
	if len(names) == 0 {
		return nil, nil, structerr.NewInvalidArgument("empty list of attribute names")
	}

	cmd, err := repo.Exec(ctx, gitcmd.Command{
		Name: "check-attr",
		Flags: []gitcmd.Option{
			gitcmd.Flag{Name: "--stdin"},
			gitcmd.Flag{Name: "-z"},
			gitcmd.ValueFlag{Name: "--source", Value: revision.String()},
		},
		Args: names,
	},
		gitcmd.WithSetupStdin(),
		gitcmd.WithSetupStdout(),
	)
	if err != nil {
		return nil, nil, fmt.Errorf("start check-attr command: %w", err)
	}

	checker := CheckAttrCmd{
		cmd:    cmd,
		stdout: bufio.NewReader(cmd),
		stdin:  bufio.NewWriter(cmd),
		count:  len(names),
		m:      &sync.Mutex{},
	}

	return &checker, func() { _ = cmd.Wait() }, nil
}
```

**File:** internal/git/gitattributes/check_attr.go (L83-104)
```go
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
