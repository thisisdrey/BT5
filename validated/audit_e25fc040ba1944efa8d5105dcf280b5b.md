### Title
Unbounded `MessageRegex`/`Author` request fields enable regex/CPU exhaustion DoS in `FindCommits` - (File: `internal/gitaly/service/commit/find_commits.go`)

### Summary
The `FindCommits` RPC accepts client-supplied `MessageRegex` and `Author` fields and forwards them unvalidated and unbounded as `--grep`/`--author` regex arguments to `git log`. No length or complexity limit is enforced before the values reach Git's regex engine, so a large or pathological pattern is evaluated by Git once per commit walked, similar to how Nextcloud's unbounded password field caused CPU/memory exhaustion in its hashing routine.

### Finding Description
`validateFindCommitsRequest` only validates the repository and the `Revision` field: [1](#0-0) . There is no check on the size or shape of `req.GetMessageRegex()` or `req.GetAuthor()`.

These fields are placed directly into the `git log` command line as regex flag values: [2](#0-1)  and [3](#0-2) .

`getLogCommandSubCmd` builds the full `git log` invocation from request fields with no bound on the regex length, and `findCommits` executes that command against the repository, streaming output back over an ordinary ACCESSOR RPC call: [4](#0-3) . Because `--grep`/`--author` are evaluated by Git's own regex matcher against every candidate commit during the traversal (bounded only by `--max-count`, which a caller also fully controls), an attacker-supplied megabyte-scale or backtracking-prone pattern is compiled and evaluated repeatedly, consuming CPU/memory on the Gitaly node for the duration of the RPC.

### Impact Explanation
An unprivileged, authenticated API caller (any user able to invoke `FindCommits`, e.g. via GitLab's commit search feature) can submit an oversized/pathological `message_regex` or `author` value. This causes the spawned `git log` process to consume excessive CPU and memory while compiling/matching the regex across the requested commit range, degrading or blocking the Gitaly worker and other requests contending for the same resources — a denial-of-service condition, mirroring the "unbounded input causes hashing/CPU exhaustion" bug class from the source report.

### Likelihood Explanation
`FindCommits` is a standard, frequently exposed ACCESSOR RPC reachable by any client with repository read access; no special privilege, token leakage, or malicious peer is required — only a crafted request field. The absence of any length/complexity validation on `MessageRegex`/`Author` makes exploitation straightforward and repeatable (e.g., issuing several concurrent requests amplifies impact).

### Recommendation
Add validation in `validateFindCommitsRequest` (and in the analogous `ListCommits`/`FindAllCommits` code paths that accept `commit_message_patterns`/`author`) to reject `MessageRegex`/`Author`/`CommitMessagePatterns` values above a reasonable maximum length, and consider compiling/sanity-checking the pattern (or imposing a timeout on the spawned `git log` process) so a single request cannot pin CPU indefinitely.

### Proof of Concept
1. As an authenticated user with read access to a repository containing a reasonably large number of commits, call `FindCommits` with `revision` set to a branch, `limit` set high, and `message_regex` set to a very long string (e.g., hundreds of thousands of characters) or a pattern crafted for catastrophic backtracking.
2. Observe that `getLogCommandSubCmd` embeds the value verbatim into `--grep=<value>` [3](#0-2) , and the spawned `git log` process consumes elevated CPU/memory while evaluating the regex against each traversed commit, with no server-side limit stopping it before completion or timeout.

### Citations

**File:** internal/gitaly/service/commit/find_commits.go (L27-35)
```go
func validateFindCommitsRequest(ctx context.Context, locator storage.Locator, in *gitalypb.FindCommitsRequest) error {
	if err := locator.ValidateRepository(ctx, in.GetRepository()); err != nil {
		return err
	}
	if err := git.ValidateRevision(in.GetRevision(), git.AllowEmptyRevision()); err != nil {
		return err
	}
	return nil
}
```

**File:** internal/gitaly/service/commit/find_commits.go (L69-90)
```go
func (s *server) findCommits(ctx context.Context, req *gitalypb.FindCommitsRequest, stream gitalypb.CommitService_FindCommitsServer) (err error) {
	opts := gitcmd.ConvertGlobalOptions(req.GetGlobalOptions())
	repo := s.localRepoFactory.Build(req.GetRepository())

	var stderr bytes.Buffer
	gitLogCmd := getLogCommandSubCmd(req)
	logCmd, err := repo.Exec(ctx, gitLogCmd, append(opts, gitcmd.WithSetupStdout(), gitcmd.WithStderr(&stderr))...)
	if err != nil {
		return fmt.Errorf("error when creating git log command: %w", err)
	}

	defer func() {
		if err = logCmd.Wait(); err != nil {
			err = wrapGitLogCmdError(req.GetRevision(), err, stderr.String())
		}
	}()

	objectReader, cancel, err := s.catfileCache.ObjectReader(ctx, repo)
	if err != nil {
		return fmt.Errorf("creating catfile: %w", err)
	}
	defer cancel()
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
