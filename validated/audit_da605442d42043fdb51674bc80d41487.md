### Title
Unbounded memory buffering in SearchFilesByName when Limit<=0 causes DoS - (File: internal/gitaly/service/repository/search_files.go)

### Summary
`SearchFilesByName` executes `git ls-tree -r -z` over the full tree and buffers every matched path into an in-memory `[][]byte` slice via `parseLsTree` before sending a single `stream.Send` at the end. When `req.GetLimit()` is 0 or negative, the only break condition `if limit > 0 && len(files) >= limit` never triggers, so the loop consumes the entire `ls-tree` output for the repository, allowing an attacker-controlled repository with a very large number of tracked paths to force the Gitaly process to allocate memory proportional to repository size rather than response size.

### Finding Description
`SearchFilesByName` [1](#0-0)  builds an `ls-tree --full-tree --name-status -r -z <ref>` command and passes the output to `parseLsTree`, which loops over `parser.NextEntryPath()` and appends every non-filtered path into the `files` slice: [2](#0-1) . The loop's only bound is `if limit > 0 && len(files) >= limit { break }` at line 215, which is a no-op whenever `limit <= 0` — this is the documented default/zero-value for the protobuf `int32 Limit` field, fully controlled by the RPC caller. `localrepo.NewParser`/`NextEntryPath` [3](#0-2)  performs no internal bounding either; it simply reads NUL-delimited paths until EOF. As a result, for a repository containing millions of tracked file paths, an unprivileged owner of that repository can call `SearchFilesByName` with `Limit=0` (or negative) and a broad `Query`/ref to force the entire path list to be materialized in memory as `files` before the single `stream.Send(&gitalypb.SearchFilesByNameResponse{Files: files})` at line 165 is ever invoked. None of the existing checks (`validateSearchFilesRequest`, filter length cap `searchFilesFilterMaxLength`) constrain the number of returned/buffered paths — the filter only bounds the regex length, not match count.

### Impact Explanation
This matches GitLab's DoS/resource-exhaustion impact class for Gitaly: a single unprivileged RPC call against attacker-owned content can drive unbounded heap growth in the Gitaly process handling the request, potentially exhausting available memory and impacting the node (and other tenants/repositories served by the same Gitaly process, since Gitaly is typically multi-tenant). The buffered response would also likely exceed gRPC's default max message size on `stream.Send`, but the OOM/allocation pressure occurs before that send happens, so the resource-exhaustion impact exists independent of the message-size error.

### Likelihood Explanation
Preconditions are within an unprivileged attacker's capability: own/push/import a repository with a very large number of tracked file paths (achievable without special roles), then invoke `SearchFilesByName` with `Limit=0` (the default zero value, requiring no special client behavior) and a `Query` matching many/most paths (e.g., empty query is rejected by `validateSearchFilesRequest`, but a broad regex/substring like a single common character works). This is fully reproducible and repeatable, and does not depend on any privileged configuration, secret, or other component's bug.

### Recommendation
Enforce a server-side maximum cap on the number of buffered/returned entries in `parseLsTree` regardless of the client-supplied `limit` (e.g., treat `limit <= 0` as "use server-side default/max" instead of "unbounded"), and/or stream results incrementally via `stream.Send` in chunks as they are parsed instead of accumulating the full `files` slice before a single send, bounding memory to the chunk size rather than to the total match count.

### Proof of Concept
```go
func TestSearchFilesByName_UnboundedMemory(t *testing.T) {
    cfg, repoProto, repoPath := setupRepositoryService(t)
    client, _ := newRepositoryClient(t, cfg)

    // Create N=10^6 tracked files in the repository (synthetic tree).
    generateManyFiles(t, repoPath, 1_000_000)

    req := &gitalypb.SearchFilesByNameRequest{
        Repository: repoProto,
        Ref:        []byte("HEAD"),
        Query:      "a", // broad query matching most paths
        Limit:      0,   // no cap enforced -> triggers unbounded buffering
    }

    var m1, m2 runtime.MemStats
    runtime.ReadMemStats(&m1)

    stream, err := client.SearchFilesByName(ctx, req)
    require.NoError(t, err)
    for {
        _, err := stream.Recv()
        if err == io.EOF {
            break
        }
        require.NoError(t, err)
    }

    runtime.ReadMemStats(&m2)
    // Assert peak allocation stays bounded (e.g., under a fixed small threshold)
    // regardless of N; with the current code this grows linearly with N.
    require.Less(t, m2.TotalAlloc-m1.TotalAlloc, uint64(50*1024*1024))
}
```
Expected on the current implementation: allocation grows proportionally with the number of matched tree entries (unbounded when `Limit=0`), violating the DoS-resilience invariant.

### Citations

**File:** internal/gitaly/service/repository/search_files.go (L111-166)
```go
func (s *server) SearchFilesByName(req *gitalypb.SearchFilesByNameRequest, stream gitalypb.RepositoryService_SearchFilesByNameServer) error {
	ctx := stream.Context()

	if err := validateSearchFilesRequest(ctx, s.locator, req); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	var filter *regexp.Regexp
	if req.GetFilter() != "" {
		if len(req.GetFilter()) > searchFilesFilterMaxLength {
			return structerr.NewInvalidArgument("filter exceeds maximum length")
		}
		var err error
		filter, err = regexp.Compile(req.GetFilter())
		if err != nil {
			return structerr.NewInvalidArgument("filter did not compile: %w", err)
		}
	}

	repo := s.localRepoFactory.Build(req.GetRepository())

	objectHash, err := repo.ObjectHash(ctx)
	if err != nil {
		return fmt.Errorf("detecting object hash: %w", err)
	}

	cmd, err := repo.Exec(ctx, gitcmd.Command{
		Name: "ls-tree",
		Flags: []gitcmd.Option{
			gitcmd.Flag{Name: "--full-tree"},
			gitcmd.Flag{Name: "--name-status"},
			gitcmd.Flag{Name: "-r"},
			// We use -z to force NULL byte termination here to prevent git from
			// quoting and escaping unusual file names. Lstree parser would be a
			// more ideal solution. Unfortunately, it supports parsing full
			// output while we are interested in the filenames only.
			gitcmd.Flag{Name: "-z"},
		},
		Args: []string{
			string(req.GetRef()),
		},
		PostSepArgs: []string{
			req.GetQuery(),
		},
	}, gitcmd.WithSetupStdout())
	if err != nil {
		return structerr.NewInternal("cmd start failed: %w", err)
	}

	files, err := parseLsTree(objectHash, cmd, filter, int(req.GetOffset()), int(req.GetLimit()))
	if err != nil {
		return err
	}

	return stream.Send(&gitalypb.SearchFilesByNameResponse{Files: files})
}
```

**File:** internal/gitaly/service/repository/search_files.go (L194-221)
```go
func parseLsTree(objectHash git.ObjectHash, cmd *command.Command, filter *regexp.Regexp, offset int, limit int) ([][]byte, error) {
	var files [][]byte
	var index int
	parser := localrepo.NewParser(cmd, objectHash)

	for {
		path, err := parser.NextEntryPath()
		if err != nil {
			if errors.Is(err, io.EOF) {
				break
			}
			return nil, err
		}
		if filter != nil && !filter.Match(path) {
			continue
		}

		index++
		if index > offset {
			files = append(files, path)
		}
		if limit > 0 && len(files) >= limit {
			break
		}
	}

	return files, nil
}
```

**File:** internal/git/localrepo/parser.go (L73-84)
```go
// NextEntryPath reads the path of next entry as it would be written by `git ls-tree --name-only -z`.
func (p *Parser) NextEntryPath() ([]byte, error) {
	treeEntryPath, err := p.reader.ReadBytes(0x00)
	if err != nil {
		if errors.Is(err, io.EOF) {
			return nil, io.EOF
		}

		return nil, fmt.Errorf("reading path: %w", err)
	}
	return treeEntryPath[:len(treeEntryPath)-1], nil
}
```
