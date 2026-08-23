### Title
Missing length bound on `Query` (regex pattern) in `SearchFilesByContent` allows resource-exhaustive `git grep -e` invocations - (File: internal/gitaly/service/repository/search_files.go)

### Summary
`validateSearchFilesRequest` only checks that `Query` is non-empty but enforces no maximum length, unlike `SearchFilesByName`'s `Filter` field which is explicitly capped by `searchFilesFilterMaxLength` (1000 bytes) before being compiled as a regexp. In `SearchFilesByContent`, the unbounded `Query` string is passed directly as the `-e` value to a spawned `git grep --perl-regexp` child process, so an attacker-controlled multi-megabyte PCRE pattern is handed straight to git without any size gate.

### Finding Description
`SearchFilesByContent` at `internal/gitaly/service/repository/search_files.go:32-66` calls `validateSearchFilesRequest` and then builds a `git grep --perl-regexp -e <Query>` command using `req.GetQuery()` unmodified: [1](#0-0) 

`validateSearchFilesRequest` (shared by both `SearchFilesByContent` and `SearchFilesByName`) only rejects an empty query: [2](#0-1) 

By contrast, `SearchFilesByName`'s `Filter` (also user-supplied and also compiled as a regexp, via Go's `regexp` package) is explicitly bounded: [3](#0-2) [4](#0-3) 

This asymmetry means an attacker can send a `SearchFilesByContentRequest` with a `Query` of several megabytes (a large, potentially pathological PCRE pattern) to their own repository. This gets passed as a command-line argument value to `git grep`, which will need to compile/evaluate that pattern via PCRE against the repository tree at `req.GetRef()`, consuming CPU and memory in the spawned git process for the duration of the RPC. Repeating this concurrently (many `SearchFilesByContentRequest`s in parallel, each own repo) multiplies the number of concurrent `git grep` child processes each carrying an oversized pattern, which can degrade CPU/memory availability on the Gitaly node for all repositories hosted there.

None of the existing generic protections stop this: `locator.ValidateRepository` only validates the repository path, not RPC payload sizes; there is no `git.ValidateRevision`-equivalent size cap for `Query`; and no gRPC server-side `MaxRecvMsgSize` override was found in `internal/gitaly/server/server.go`, meaning the effective per-message ceiling is the grpc-go default (4 MiB), which still permits multi-megabyte `Query` values well beyond what a legitimate search pattern would need.

### Impact Explanation
This is a denial-of-service class issue (CPU/memory exhaustion via oversized regex compilation/matching in git-grep child processes), scoped to the node hosting the attacker's own and other tenants' repositories. Because Gitaly is shared across many repositories/projects on a node, exhausting CPU/memory via concurrent oversized-pattern `git grep` invocations can degrade or deny service to unrelated repositories -- matching the "denial of service" impact class. It does not grant data disclosure, git object access outside the repo, or command injection; the impact is limited to availability/resource exhaustion, and is capped by whatever default gRPC message size limit applies (~4 MiB) and by the RPC's `git.ValidateRevision`/`command` timeout and concurrency limiter middleware (`limithandler`), which may partially mitigate but does not close the gap since no explicit `Query` length cap exists.

### Likelihood Explanation
The precondition is minimal: any unprivileged user who can call `SearchFilesByContent` against their own repository (a routine, unauthenticated-by-role RPC available to normal GitLab users doing code search) can trivially construct a several-megabyte `Query` string and send it, then repeat this concurrently at will. This is straightforward and fully attacker-controlled -- no special role, secret, or non-default configuration is required. The main uncertainty is the magnitude of achievable resource exhaustion, which depends on the concurrency limiter/middleware configuration (`internal/grpc/middleware/limithandler`) and default gRPC message-size ceiling, which were not fully confirmed to be tuned to prevent abuse at scale.

### Recommendation
Add an explicit maximum length check for `Query` in `validateSearchFilesRequest` (or specifically in `SearchFilesByContent`) analogous to `searchFilesFilterMaxLength`, e.g., reject `Query` longer than a bounded constant (such as 1000 bytes, matching `SearchFilesByName`'s filter bound) before constructing the `git grep` command, returning `structerr.NewInvalidArgument` on violation.

### Proof of Concept
```go
func TestSearchFilesByContent_OversizedQuery(t *testing.T) {
    cfg, repoProto, _ := setupRepositoryService(t) // existing test helper
    client := newRepositoryClient(t, cfg) // existing test helper

    hugeQuery := strings.Repeat("a", 5*1024*1024) // 5MB PCRE pattern
    stream, err := client.SearchFilesByContent(ctx, &gitalypb.SearchFilesByContentRequest{
        Repository: repoProto,
        Ref:        []byte("master"),
        Query:      hugeQuery,
    })
    require.NoError(t, err)

    // Currently: request is accepted and a git grep process is spawned with
    // the 5MB pattern, consuming excess CPU/memory instead of being rejected
    // as InvalidArgument like SearchFilesByName does for oversized Filter.
    _, err = stream.Recv()
    // Expected after fix: InvalidArgument "query exceeds maximum length"
    // Actual before fix: proceeds to spawn git grep with the huge -e value
}
```
Repeating this request concurrently (N goroutines) while monitoring `git grep` child process CPU/RSS demonstrates the resource-exhaustion effect described.

### Citations

**File:** internal/gitaly/service/repository/search_files.go (L25-27)
```go
	// searchFilesFilterMaxLength controls the maximum length of the regular
	// expression to thwart excessive resource usage when filtering
	searchFilesFilterMaxLength = 1000
```

**File:** internal/gitaly/service/repository/search_files.go (L41-56)
```go
	cmd, err := repo.Exec(ctx, gitcmd.Command{
		Name: "grep",
		Flags: []gitcmd.Option{
			gitcmd.Flag{Name: "--ignore-case"},
			gitcmd.Flag{Name: "-I"},
			gitcmd.Flag{Name: "--line-number"},
			gitcmd.Flag{Name: "--null"},
			gitcmd.ValueFlag{Name: "--before-context", Value: surroundContext},
			gitcmd.ValueFlag{Name: "--after-context", Value: surroundContext},
			gitcmd.Flag{Name: "--perl-regexp"},
			gitcmd.ValueFlag{Name: "-e", Value: req.GetQuery()},
		},
		Args: []string{
			string(req.GetRef()),
		},
	}, gitcmd.WithSetupStdout())
```

**File:** internal/gitaly/service/repository/search_files.go (L118-128)
```go
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
```

**File:** internal/gitaly/service/repository/search_files.go (L174-192)
```go
func validateSearchFilesRequest(ctx context.Context, locator storage.Locator, req searchFilesRequest) error {
	if err := locator.ValidateRepository(ctx, req.GetRepository()); err != nil {
		return err
	}

	if len(req.GetQuery()) == 0 {
		return errors.New("no query given")
	}

	if len(req.GetRef()) == 0 {
		return errors.New("no ref given")
	}

	if bytes.HasPrefix(req.GetRef(), []byte("-")) {
		return errors.New("invalid ref argument")
	}

	return nil
}
```
