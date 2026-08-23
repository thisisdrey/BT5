### Title
Unbounded `--perl-regexp` query in `SearchFilesByContent` enables ReDoS-based DoS - ([File: internal/gitaly/service/repository/search_files.go])

### Finding Description
`SearchFilesByContent` builds a `git grep --perl-regexp -e <query>` command where `req.GetQuery()` is taken verbatim from the RPC request and passed as the value of the `-e` flag [1](#0-0) . The only validation performed on the query is a non-empty check in `validateSearchFilesRequest`; there is no length or complexity limit applied to it [2](#0-1) . This is inconsistent with the sibling RPC `SearchFilesByName`, whose `Filter` field is explicitly capped at `searchFilesFilterMaxLength` (1000 chars) before being compiled as a Go regex [3](#0-2) .

Because `--perl-regexp` uses PCRE-style backtracking, a user who fully controls both the repository content (via push/import, since they own the repo) and the `Query` field can craft a classic catastrophic-backtracking pattern (e.g. `(a+)+$` or `(a|a)*b`) together with a matching worst-case input string committed to a file in their own repository. When `SearchFilesByContent` is invoked with that pattern against that repository, the spawned `git grep` process can be driven into exponential-time backtracking, pinning a CPU core for an extended period per request, and this can be repeated/parallelized by the attacker to exhaust Gitaly worker capacity — a resource-exhaustion DoS of the RPC handler and the node serving it.

The `Flag`/`ValueFlag` sanitization in `gitcmd` only guards the flag *names* against injection via `flagRegex`, not the arbitrary `Value` content [4](#0-3) , so there's no protection at that layer either — it is orthogonal to argument-injection concerns and does not address ReDoS.

### Impact Explanation
This maps to GitLab's "Denial of Service" bounty impact class: an unprivileged user who owns/controls a repository can cause a Gitaly worker to burn CPU for extended periods per crafted `SearchFilesByContent` call, and by issuing multiple concurrent requests can degrade or exhaust Gitaly's process/CPU capacity, affecting other tenants on shared infrastructure.

### Likelihood Explanation
The attacker only needs standard unprivileged capabilities already assumed: push content to an owned repository and issue the `SearchFilesByContent` RPC (reachable via GitLab's code-search "search in this repository" feature) with an attacker-chosen `Query`. No special role, config, or secret is required, and the attack is trivially repeatable/scriptable.

### Recommendation
- Enforce a maximum length on `req.GetQuery()` in `validateSearchFilesRequest` (or a dedicated check in `SearchFilesByContent`), similar to `searchFilesFilterMaxLength` for `SearchFilesByName`.
- Consider bounding `git grep` execution time (context timeout) specific to this RPC, or switch/limit to a non-backtracking regex engine mode where feasible, to cap worst-case CPU consumption independent of query length.

### Proof of Concept
1. Push to an attacker-owned repository a text file containing a long line such as `"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaac"` (no trailing match character to force worst-case backtracking).
2. Issue `SearchFilesByContentRequest{Repository: <attacker repo>, Ref: []byte("HEAD"), Query: "(a+)+$"}`.
3. Observe the `git grep --perl-regexp -e "(a+)+$"` child process consuming a full CPU core for a duration disproportionate to input size, blocking the Gitaly worker/goroutine for that request; repeating the call concurrently multiplies the resource consumption, demonstrating handler-level DoS.

### Citations

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

**File:** internal/git/gitcmd/command_options.go (L96-102)
```go
// OptionArgs returns an error if the flag is not sanitary
func (vf ValueFlag) OptionArgs() ([]string, error) {
	if !flagRegex.MatchString(vf.Name) {
		return nil, fmt.Errorf("value flag %q failed regex validation: %w", vf.Name, ErrInvalidArg)
	}
	return []string{vf.Name, vf.Value}, nil
}
```
