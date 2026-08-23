### Title
Unbounded `exclude` List in `GetArchive` Causes Resource-Exhaustion DoS - (File: internal/gitaly/service/repository/archive.go)

### Summary
`RepositoryService.GetArchive` accepts a repeated `exclude` field (and a `path` field) of unbounded size and count from any client authorized to call the RPC on a repository they can access. Neither the request validation nor the precondition-checking code imposes a limit on the number of entries in `exclude`, and each entry triggers an independent, non-trivial Git object lookup before the archive command is even executed.

### Finding Description
In `GetArchive` [1](#0-0) , the `exclude` repeated field from `GetArchiveRequest` is copied into a local slice with only per-entry relative-path validation (`storage.ValidateRelativePath`), but no cap on the number of elements: [2](#0-1) 

That unbounded slice is then passed into `validateGetArchivePrecondition`, which iterates over every exclude entry and performs a `catfile.TreeEntryFinder.FindByRevisionAndPath` lookup per entry — an object-database walk that is comparatively expensive (potentially a tree traversal down to the requested path) for each item: [3](#0-2) 

After this loop, all validated `exclude` entries are turned into `:(exclude)` pathspec arguments and passed as `PostSepArgs` directly to the `git archive` command line in `handleArchive`: [4](#0-3) [5](#0-4) 

Because `PostSepArgs` bypasses the dash-prefix/positional-argument validation applied to `Args` (per `internal/git/gitcmd/command.go`'s documented intent) and there is no limit on the number or aggregate size of these arguments, a caller can submit a `GetArchiveRequest` with an extremely large `exclude` list (tens of thousands of entries, each a moderately long path string). This causes:
- A costly, unbounded sequence of catfile object lookups per request before the archive is even generated (CPU/IO amplification), and
- A very large `argv`/pathspec list handed to the `git archive` subprocess, risking process-level argument-size limits or excessive memory during pathspec expansion.

This matches the reported bug class: "no limits on the size of text field inputs" — here applied to a repeated `bytes` RPC field rather than an HTTP form field, but reachable the same way from an ordinary authenticated Gitaly client (e.g., via GitLab Rails/Workhorse forwarding an archive-download request).

### Impact Explanation
An ordinary user who can trigger a `GetArchive` call for a repository (e.g., via the "Download source code" feature, which GitLab forwards to Gitaly) can cause the Gitaly node handling the RPC to spend disproportionate CPU and memory processing the exclude list before any actual archive work begins, and potentially destabilize the `git archive` subprocess invocation. Since `GetArchive` runs per-repository but Gitaly is a shared multi-tenant service, this can degrade or deny service to other repositories/users on the same node (DoS of a handler).

### Likelihood Explanation
Likelihood is moderate-to-high: the RPC is reachable by any user with read access to a repository (an ordinary, unprivileged actor), requires no special timing or race condition, and only requires constructing a request with a large repeated field — something achievable via any gRPC client capable of talking to Gitaly (directly, or by a modified/compromised Rails-side request if input length isn't separately capped there). No authentication bypass or privilege escalation is required, only volume of a legitimately-typed field.

### Recommendation
- Impose an explicit maximum count (and maximum aggregate byte size) on `GetArchiveRequest.exclude` (and consider similar limits on `path`), rejecting oversized requests early with `InvalidArgument` before any catfile lookups or command construction occur.
- Apply the same policy consistently to other unbounded repeated fields used to build Git command arguments across the codebase (e.g., other RPCs that build `PostSepArgs`/pathspecs from repeated request fields), per the general recommendation to validate input size both client- and server-side.
- Consider validating cumulative request size (e.g., via a byte-size accounting pass over `exclude`) rather than count alone, since a small number of very large strings could achieve the same effect.

### Proof of Concept
1. As an authenticated ordinary user with read access to a repository, call `RepositoryService.GetArchive` with:
   - `repository`: a valid, accessible repository,
   - `commit_id`: a valid commit,
   - `format`: `TAR`,
   - `exclude`: an array of ~100,000 distinct valid relative paths (or as many as the gRPC message-size limit allows) that exist in the tree.
2. Observe that `validateGetArchivePrecondition` performs one `FindByRevisionAndPath` catfile lookup per exclude entry sequentially, consuming significant CPU/time on the Gitaly node before the archive command is even started.
3. Repeat the request concurrently from multiple sessions to amplify resource consumption on the shared Gitaly node, degrading service for other repositories.

### Citations

**File:** internal/gitaly/service/repository/archive.go (L34-59)
```go
func (s *server) GetArchive(in *gitalypb.GetArchiveRequest, stream gitalypb.RepositoryService_GetArchiveServer) error {
	ctx := stream.Context()
	repository := in.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}
	compressArgs, format := parseArchiveFormat(in.GetFormat())
	repo := s.localRepoFactory.Build(repository)

	repoRoot, err := repo.Path(ctx)
	if err != nil {
		return err
	}

	path, err := storage.ValidateRelativePath(repoRoot, string(in.GetPath()))
	if err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	exclude := make([]string, len(in.GetExclude()))
	for i, ex := range in.GetExclude() {
		exclude[i], err = storage.ValidateRelativePath(repoRoot, string(ex))
		if err != nil {
			return structerr.NewInvalidArgument("%w", err)
		}
	}
```

**File:** internal/gitaly/service/repository/archive.go (L172-178)
```go
	for i, exclude := range exclude {
		if ok, err := findGetArchivePath(ctx, f, commitID, exclude); err != nil {
			return err
		} else if !ok {
			return structerr.NewFailedPrecondition("exclude[%d] doesn't exist", i)
		}
	}
```

**File:** internal/gitaly/service/repository/archive.go (L195-212)
```go
func (s *server) handleArchive(ctx context.Context, p archiveParams) error {
	var args []string
	pathspecs := make([]string, 0, len(p.exclude)+1)
	if !p.in.GetElidePath() {
		// git archive [options] <commit ID> -- <path> [exclude*]
		args = []string{p.in.GetCommitId()}
		pathspecs = append(pathspecs, p.archivePath)
	} else if p.archivePath != "." {
		// git archive [options] <commit ID>:<path> -- [exclude*]
		args = []string{p.in.GetCommitId() + ":" + p.archivePath}
	} else {
		// git archive [options] <commit ID> -- [exclude*]
		args = []string{p.in.GetCommitId()}
	}

	for _, exclude := range p.exclude {
		pathspecs = append(pathspecs, ":(exclude)"+exclude)
	}
```

**File:** internal/gitaly/service/repository/archive.go (L246-251)
```go
		archiveCommand, err := repo.Exec(ctx, gitcmd.Command{
			Name:        "archive",
			Flags:       []gitcmd.Option{gitcmd.ValueFlag{Name: "--format", Value: p.format}, gitcmd.ValueFlag{Name: "--prefix", Value: p.in.GetPrefix() + "/"}},
			Args:        args,
			PostSepArgs: pathspecs,
		}, gitcmd.WithEnv(env...), gitcmd.WithConfig(gitConfig...), gitcmd.WithSetupStdout())
```
