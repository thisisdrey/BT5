### Title
DoS via unbounded `--perl-regexp` query in `SearchFilesByContent` - (File: internal/gitaly/service/repository/search_files.go)

### Summary
`SearchFilesByContent` passes the caller-supplied `Query` field directly as a `git grep --perl-regexp` pattern with no length limit and no complexity screening, unlike the sibling `SearchFilesByName` RPC, which already caps its `Filter` regex at `searchFilesFilterMaxLength` (1000 bytes) specifically "to thwart excessive resource usage." [1](#0-0)  `git grep --perl-regexp` uses a PCRE-compatible, backtracking regex engine (the same general bug class as Ruby's `Onigmo` engine referenced in the report), so a crafted pattern with nested/ambiguous quantifiers can trigger catastrophic backtracking while git greps repository content.

### Finding Description
`SearchFilesByContent` builds a `git grep` invocation using `req.GetQuery()` verbatim as the `-e` pattern with `--perl-regexp` enabled: [2](#0-1)  The only validation applied is `validateSearchFilesRequest`, which merely checks that repository/ref/query are non-empty and that `ref` doesn't start with `-`; it performs no length cap or regex-safety check on `Query`: [3](#0-2) 

By contrast, `SearchFilesByName`'s `Filter` field — which is compiled with Go's RE2-based `regexp` package (linear-time, immune to catastrophic backtracking) — is still defensively capped at 1000 bytes: [4](#0-3)  This asymmetry indicates the length-limit protection was added for the RE2 path but never extended to the PCRE (`--perl-regexp`) path used by `SearchFilesByContent`, which is actually the one running an exponential-worst-case engine.

Since `git grep` scans the full blob content of every file at the given ref, an attacker-supplied pathological PCRE pattern (e.g. deeply nested optional/repeated groups) combined with matching or near-matching content in the repository forces `git-grep`'s regex engine into exponential backtracking, burning CPU for the lifetime of the RPC.

### Impact Explanation
Any authenticated/unprivileged user who can invoke `SearchFilesByContent` against a repository they can read (this RPC is reachable via GitLab's standard code-search feature) can supply a crafted `Query` to make the resulting `git-grep --perl-regexp` process consume large amounts of CPU. As with the original report, several parallel requests could exhaust CPU on the Gitaly node, degrading service for other repositories sharing that host — an uncontrolled resource consumption / DoS matching CWE-400.

### Likelihood Explanation
Moderate-to-high. No privilege beyond normal repository-read access is required, the field is a free-form string with no server-side sanitization or length bound, and repository content is fully attacker-controllable (a user can push a file whose content is designed to maximize backtracking against their own crafted query, then invoke the search RPC on their own project).

### Recommendation
Apply the same `searchFilesFilterMaxLength`-style cap (or a stricter bound) to `SearchFilesByContentRequest.Query`, and/or enforce a bound via a request-scoped timeout/`--threads`-limited grep invocation to prevent unbounded PCRE backtracking. Consider disallowing `--perl-regexp` in favor of `-E`/basic regex, or pre-validating the query with a backtracking-safe engine (RE2) before invoking `git grep`.

### Proof of Concept
1. As an ordinary user, push a file to a repo whose content is crafted to maximize PCRE backtracking against a pattern such as `(a+)+$` combined with a long string of `a`s followed by a non-matching character (e.g., `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa!`).
2. Call `RepositoryService.SearchFilesByContent` with `Query = "(a+)+$"`, `Ref = "<branch>"`.
3. Observe `git grep --perl-regexp -e "(a+)+$"` consuming CPU for an extended period while scanning the crafted file, unbounded by any query-length or complexity check on the Gitaly side.

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
