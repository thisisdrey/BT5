### Title
Unbounded User-Controlled Regex Passed to Git's Native Regex Engine Enables ReDoS/CPU-Exhaustion DoS - (File: internal/gitaly/service/commit/find_commits.go, internal/gitaly/service/commit/commits_by_message.go, internal/git/gitpipe/revision.go)

### Summary
`FindCommits`, `CommitsByMessage`, and `ListCommits` RPCs accept fully attacker-controlled regular expression strings (`message_regex`, `query`, `author`, `commit_message_patterns`) and pass them unsanitized and unbounded as `--grep=<pattern>` / `--author=<pattern>` arguments to `git log`/`git rev-list`. Git's grep/log regex matching (POSIX extended or PCRE, depending on build/config) uses backtracking-capable regex engines that are susceptible to the same catastrophic-backtracking class of bug described in the external report (Rack's `RFC2183`), except here the vulnerable engine is invoked inside the spawned `git` subprocess rather than in Gitaly's own Go code.

### Finding Description
- `getLogCommandSubCmd` builds the `git log` command and, if `req.GetMessageRegex() != ""`, adds it verbatim as the value of `--grep`, plus `--regexp-ignore-case`: [1](#0-0) 
- `commitsByMessage` does the same with the `Query` field, again forwarded straight into `--grep=`: [2](#0-1) 
- `FindCommits` also forwards the `Author` field directly into `--author=<pattern>`, which git also matches as a regex against every commit's author line: [3](#0-2) 
- `ListCommits` funnels `Author`, `IgnoreCase`, and `CommitMessagePatterns` from the request into the `gitpipe.Revlist` pipeline (`WithAuthor`, `WithIgnoreCase`, `WithCommitMessagePatterns`), which is documented as causing `git-rev-list(1)` to apply these values as regex patterns against every commit message/author in the walked history: [4](#0-3) [5](#0-4) [6](#0-5) 

Unlike Gitaly's own Go-side regexes (e.g., `statsPattern`, `ambiguousArgRegex`, `hostPattern`), which are compiled with Go's `regexp` package (RE2 engine, immune to catastrophic backtracking) as seen throughout the codebase, e.g.: [7](#0-6) 
these user-supplied patterns are never evaluated by Go's regex engine at all — they are handed off as opaque strings to the `git` binary's own regex matcher, over which Gitaly has no ReDoS protection. There is no length limit, complexity check, or timeout specific to these fields; validation only checks that `Query`/`MessageRegex` are non-empty or that the repository/revision is valid: [8](#0-7) [9](#0-8) 

A crafted pathological regex (nested quantifiers/alternations, e.g. `(a+)+$`-style patterns or long grep pattern combinations known to trigger super-linear behavior in glibc/PCRE-backed matchers) supplied via any of these fields, combined with a repository containing enough commits/long commit messages, can cause the spawned `git log`/`git rev-list` process to consume excessive CPU for a very long time while matching against each commit's message/author line.

### Impact Explanation
This is reachable by any ordinary, authenticated Gitaly client issuing a normal `FindCommits`, `CommitsByMessage`, or `ListCommits` RPC — no special privileges, tokens, or peer compromise required. A single crafted request can pin a CPU core on the Gitaly node for the duration of the git subprocess, and repeated requests can exhaust available CPU/goroutine/process capacity, degrading or denying service for the storage shard hosting the affected repository (and, depending on git's per-process resource usage, potentially co-located repositories on the same node). This matches the "DoS of a handler" outcome accepted by the validation criteria.

### Likelihood Explanation
Likelihood is high: `message_regex`, `query`, `author`, and `commit_message_patterns` are ordinary, unauthenticated-of-privilege client-supplied fields on commonly used, non-privileged RPCs (`FindCommits`, `CommitsByMessage`, `ListCommits`). No secondary conditions (e.g., specific repository state beyond having some commit history) are required beyond a normal push/commit history that most repositories already have.

### Recommendation
- Bound the length and complexity of user-supplied `message_regex`/`query`/`author`/`commit_message_patterns` fields before forwarding them to git (e.g., enforce a maximum pattern length, reject patterns with excessive nested quantifiers, or run a static ReDoS-pattern detector).
- Where feasible, validate/compile the supplied pattern using Go's own `regexp` package (RE2, linear-time) first and reject the request if it fails to compile as a safe RE2 pattern, rather than relying on git's own backtracking regex engine for arbitrary user input.
- Enforce a hard execution/CPU timeout on the spawned `git log`/`git rev-list` process specifically when `--grep`/`--author` are user-controlled, independent of the overall RPC deadline, and kill+error out if exceeded.
- Consider disabling PCRE-based grep matching (`grep.patternType`) for these code paths so only the more analysis-tractable POSIX extended engine is used, and document/pin whichever engine is chosen.

### Proof of Concept
1. Create or use a test repository with a moderate number of commits.
2. Call `FindCommits` (or `CommitsByMessage`/`ListCommits`) with `message_regex` (or `query`/`author`/`commit_message_patterns`) set to a classic catastrophic-backtracking pattern, e.g.:
   `(a|aa)+$` or `(.*)*!` style patterns, or the more general nested-alternation constructs shown in the original Rack report adapted to POSIX/PCRE grammar supported by the installed git's regex engine.
3. Observe that the `git log`/`git rev-list` child process spawned by Gitaly spikes CPU usage and does not return within a normal response time, while repeated concurrent requests amplify CPU consumption on the Gitaly node.
4. Compare against a request using a benign pattern of similar length to confirm the slowdown is due to regex complexity rather than repository size alone.

### Citations

**File:** internal/gitaly/service/commit/find_commits.go (L25-25)
```go
var statsPattern = regexp.MustCompile(`\s(\d+)\sfiles? changed(,\s(\d+)\sinsertions?\(\+\))?(,\s(\d+)\sdeletions?\(-\))?`)
```

**File:** internal/gitaly/service/commit/find_commits.go (L27-34)
```go
func validateFindCommitsRequest(ctx context.Context, locator storage.Locator, in *gitalypb.FindCommitsRequest) error {
	if err := locator.ValidateRepository(ctx, in.GetRepository()); err != nil {
		return err
	}
	if err := git.ValidateRevision(in.GetRevision(), git.AllowEmptyRevision()); err != nil {
		return err
	}
	return nil
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

**File:** internal/git/gitpipe/revision.go (L196-218)
```go
// WithAuthor will cause git-rev-list(1) to only show commits created by an author matching the
// given pattern.
func WithAuthor(author []byte) RevlistOption {
	return func(cfg *revlistConfig) {
		cfg.author = author
	}
}

// WithIgnoreCase causes git-rev-list(1) to apply regex patterns
// in case-insensitive manner.
func WithIgnoreCase(ignoreCase bool) RevlistOption {
	return func(cfg *revlistConfig) {
		cfg.regexIgnoreCase = ignoreCase
	}
}

// WithCommitMessagePatterns causes git-rev-list(1) to only show commits whose message
// matches any of the regex patterns in commitMessagePatterns.
func WithCommitMessagePatterns(commitMessagePatterns [][]byte) RevlistOption {
	return func(cfg *revlistConfig) {
		cfg.commitMessagePatterns = commitMessagePatterns
	}
}
```

**File:** internal/gitaly/service/commit/list_commits.go (L152-157)
```go
	if len(request.GetAuthor()) != 0 {
		revlistOptions = append(revlistOptions, gitpipe.WithAuthor(request.GetAuthor()))
	}

	if request.GetIgnoreCase() {
		revlistOptions = append(revlistOptions, gitpipe.WithIgnoreCase(request.GetIgnoreCase()))
```

**File:** internal/gitaly/service/commit/list_commits.go (L185-187)
```go
	if len(request.GetCommitMessagePatterns()) > 0 {
		revlistOptions = append(revlistOptions, gitpipe.WithCommitMessagePatterns(request.GetCommitMessagePatterns()))
	}
```
