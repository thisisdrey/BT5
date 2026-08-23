## Title
DoS via unbounded regex in `SearchFilesByContent` — (File: `internal/gitaly/service/repository/search_files.go`)

### Summary
`RepositoryService.SearchFilesByContent` passes the client-supplied `Query` field directly, unvalidated for length or complexity, as the `-e` argument to `git grep --perl-regexp`. Unlike its sibling RPC `SearchFilesByName`, which enforces a `searchFilesFilterMaxLength` (1000 chars) on its `Filter` regex before compiling it, `SearchFilesByContent`'s `Query` has no equivalent bound. A crafted PCRE pattern with catastrophic backtracking potential can therefore be handed straight to `git grep`, burning CPU on the Gitaly node for the full duration of the RPC — directly analogous to the reported Banzai/`preview_markdown` issue where an attacker-controlled string was fed into an expensive text-processing routine without any size/complexity guard.

### Finding Description
`SearchFilesByContent` validates the request via `validateSearchFilesRequest`, which only checks that `Query` is non-empty and that `Ref` is present/safe: [1](#0-0) 

It then builds and executes the git command using the raw, unbounded `Query` as a PCRE pattern: [2](#0-1) 

By contrast, `SearchFilesByName` explicitly guards against this exact class of resource-consumption issue for its own regex field (`Filter`), capping it at `searchFilesFilterMaxLength = 1000` bytes before compilation: [3](#0-2) [4](#0-3) 

This asymmetry shows the length/complexity check was a deliberate mitigation added for one regex-consuming field but omitted for the other. Since `--perl-regexp` invokes PCRE, patterns with nested/overlapping quantifiers (e.g., `(a+)+b`) can trigger exponential backtracking against attacker-controlled repository content, and an attacker fully controls both the pattern (`Query`) and, via a prior push, the searched content (`Ref`/blob contents) — no privileged access is required beyond ordinary repository access.

### Impact Explanation
An authenticated user with read access to any repository can call `SearchFilesByContent` with a pathological PCRE pattern, causing `git grep` — and the Gitaly worker thread waiting on it — to burn a CPU core for an extended period, up to request/RPC timeout. Multiple concurrent invocations (or repeated single-repo calls, since Gitaly's per-repo concurrency limiting targets specific RPCs like `PostUploadPackWithSidechannel` and is not applied here by default) allow multiplying the effect across CPUs, degrading or exhausting the shared Gitaly node capacity for many repositories, which mirrors the "Uncontrolled Resource Consumption" weakness class and impact scope in the source report.

### Likelihood Explanation
High. The RPC is reachable by any ordinary authenticated GitLab user through normal code-search functionality (search-by-content in a repository), requires no special privileges, no malicious peer/node, and no leaked token. The only requirement is supplying a crafted regex string in a single RPC field, which is trivial to construct.

### Recommendation
Apply the same safeguard already used for `SearchFilesByName`'s `Filter` to `SearchFilesByContent`'s `Query`: enforce a maximum length (e.g., reuse `searchFilesFilterMaxLength`) and/or reject patterns matching known catastrophic-backtracking shapes before invoking `git grep --perl-regexp`. Additionally, consider bounding `git grep` execution time/CPU via a per-command timeout or cgroup-based resource limit, and add `SearchFilesByContent`/`SearchFilesByName` to Gitaly's `[[concurrency]]` limiter configuration so repeated invocations cannot be trivially parallelized to exhaust the node.

### Proof of Concept
1. As an authenticated user with access to a repository, issue a `SearchFilesByContentRequest` with:
   - `Repository`: any accessible repo
   - `Ref`: an existing branch/ref containing at least one text file
   - `Query`: a PCRE pattern prone to catastrophic backtracking, e.g. `(a+)+$` or `(.*a){20,}` repeated, targeting content the attacker controls (e.g., a file pushed earlier containing many `a` characters without a trailing match)
2. `git grep --perl-regexp -e '(a+)+$' <ref>` is executed by Gitaly with no bound on `Query` length/complexity.
3. Observe high, sustained CPU usage on the Gitaly node for the duration of the request; issuing several such requests in parallel against different repositories multiplies CPU consumption, analogous to the original report's demonstrated server-wide impact.

### Citations

**File:** internal/gitaly/service/repository/search_files.go (L22-28)
```go
const (
	surroundContext = "2"

	// searchFilesFilterMaxLength controls the maximum length of the regular
	// expression to thwart excessive resource usage when filtering
	searchFilesFilterMaxLength = 1000
)
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
