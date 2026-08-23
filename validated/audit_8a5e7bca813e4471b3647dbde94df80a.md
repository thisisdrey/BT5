## Finding: Unbounded, Unsanitized Regex in `git log --grep=` Enables ReDoS via `CommitsByMessage`/`FindCommits` RPCs

### Title
Denial of Service via Uncontrolled Regex Passed to `git log --grep=` in `CommitsByMessage` / `FindCommits` — (File: `internal/gitaly/service/commit/commits_by_message.go`, `internal/gitaly/service/commit/find_commits.go`)

### Summary
The HackerOne report describes a DoS caused by attacker-controlled input being embedded, without sanitization or complexity limits, directly into a filter expression evaluated by a backend engine (LDAP). The analogous pattern in Gitaly is `CommitsByMessageRequest.Query` and `FindCommitsRequest.MessageRegex`, which are concatenated verbatim into a `--grep=<value>` flag executed by `git log`, with no length cap and no complexity validation, unlike the sibling `SearchFilesByName` RPC which explicitly enforces `searchFilesFilterMaxLength`.

### Finding Description
In `commitsByMessage`, the client-supplied `Query` string is inserted directly into a git command flag: [1](#0-0) 

The only validation performed is that the query is non-empty: [2](#0-1) 

Similarly, `FindCommits` builds its `git log` invocation using `req.GetMessageRegex()` as the value for a `--grep` `ValueFlag`, again with no length or complexity restriction: [3](#0-2) 

and `req.GetAuthor()` is similarly interpolated as an `--author=` regex flag with no bound: [4](#0-3) 

`git log --grep`/`--author` are evaluated by git's own regex engine (POSIX extended regex, or PCRE if compiled in), which — unlike Go's linear-time RE2 engine used elsewhere in this codebase — is susceptible to catastrophic backtracking on crafted patterns (e.g. nested quantifiers like `(a+)+$`). By contrast, the codebase already recognizes this class of risk in `SearchFilesByName`, where the filter length is explicitly capped: [5](#0-4) [6](#0-5) 

No equivalent length or pattern-complexity guard exists for `--grep`/`--author` inputs reaching `git log`.

### Impact Explanation
Any authenticated user able to invoke `CommitsByMessage` or `FindCommits` on a repository (a normal, unprivileged RPC surface used for browsing commit history) can submit a pathological regex as `Query`/`MessageRegex`/`Author`. Git's regex evaluation against the repository's commit history can then consume excessive CPU time on the Gitaly node, tying up the git subprocess and the RPC handler for an extended period. Because these RPCs are invoked per ordinary browsing/search operations, this can degrade or deny service for the storage shard hosting the repository, affecting other tenants co-located on the same Gitaly node.

### Likelihood Explanation
Likelihood is high: the RPCs are part of normal, unprivileged commit-browsing functionality, require no special repository permissions beyond read access, and no validation blocks large or adversarially constructed regex patterns before they reach `git log`.

### Recommendation
- Impose a maximum length on `Query`, `MessageRegex`, and `Author` fields analogous to `searchFilesFilterMaxLength` in `search_files.go`.
- Consider validating/rejecting patterns with excessive nested quantifiers, or running `git log --grep`/`--author` invocations under a bounded CPU/time budget (e.g., context timeout or `ulimit`-style resource constraint) so a pathological regex cannot stall the handler indefinitely.
- Where feasible, prefer git's fixed-string (`--fixed-strings`) or basic literal matching by default, only opting into full regex when explicitly required, to reduce backtracking exposure.

### Proof of Concept
1. Create or use an existing repository with a moderate commit history.
2. Call `CommitsByMessage` with `Query` set to a catastrophic-backtracking pattern, e.g. `(.*)*.*!` or `(a+)+$` repeated/nested to maximize backtracking.
3. Observe that the underlying `git log --grep=<pattern>` process consumes disproportionate CPU/time relative to input size, blocking the RPC handler and the associated git subprocess slot.
4. Repeat concurrently to exhaust available git subprocess/resource slots on the Gitaly node, denying service to other requests.

### Citations

**File:** internal/gitaly/service/commit/commits_by_message.go (L46-49)
```go
	gitLogExtraOptions := []gitcmd.Option{
		gitcmd.Flag{Name: "--grep=" + in.GetQuery()},
		gitcmd.Flag{Name: "--regexp-ignore-case"},
	}
```

**File:** internal/gitaly/service/commit/commits_by_message.go (L74-88)
```go
func validateCommitsByMessageRequest(ctx context.Context, locator storage.Locator, in *gitalypb.CommitsByMessageRequest) error {
	if err := locator.ValidateRepository(ctx, in.GetRepository()); err != nil {
		return err
	}

	if err := git.ValidateRevision(in.GetRevision(), git.AllowEmptyRevision()); err != nil {
		return err
	}

	if in.GetQuery() == "" {
		return fmt.Errorf("empty Query")
	}

	return nil
}
```

**File:** internal/gitaly/service/commit/find_commits.go (L271-273)
```go
	if req.GetAuthor() != nil {
		subCmd.Flags = append(subCmd.Flags, gitcmd.Flag{Name: fmt.Sprintf("--author=%s", string(req.GetAuthor()))})
	}
```

**File:** internal/gitaly/service/commit/find_commits.go (L311-314)
```go
	if req.GetMessageRegex() != "" {
		subCmd.Flags = append(subCmd.Flags, gitcmd.ValueFlag{Name: "--grep", Value: req.GetMessageRegex()})
		subCmd.Flags = append(subCmd.Flags, gitcmd.Flag{Name: "--regexp-ignore-case"})
	}
```

**File:** internal/gitaly/service/repository/search_files.go (L22-28)
```go
const (
	surroundContext = "2"

	// searchFilesFilterMaxLength controls the maximum length of the regular
	// expression to thwart excessive resource usage when filtering
	searchFilesFilterMaxLength = 1000
)
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
