### Title
`filterGitConfig` uses substring match instead of exact key match, allowing arbitrary `core.*` config lines to survive the snapshot config allow-list filter - ([File: internal/git/localrepo/snapshot.go])

### Summary
`filterGitConfig` is meant to strip a repository's `config` file down to only `core.repositoryFormatVersion` and the `[extensions]` section before it is included in a `GetSnapshot` tar stream, but the `core` section check uses `bytes.Contains` on the entire trimmed line rather than validating the key name exactly. An attacker who controls their own repository's git config (a normal, unprivileged operation) can craft a `core.*` key/value pair whose line text merely *contains* the substring `repositoryformatversion` anywhere (e.g., in the value) to make an otherwise-blocked line, such as `hooksPath = ...`, pass through unfiltered.

### Finding Description
The filter logic is: [1](#0-0) 

For the `core` section it only checks `bytes.HasPrefix(trimmed, []byte("["))` (section headers) or `bytes.Contains(bytes.ToLower(trimmed), []byte("repositoryformatversion"))`. This check operates on the *entire raw line*, not on a parsed key name. Consequently, a `core.<key> = <value>` line is retained if the substring "repositoryformatversion" appears **anywhere** in that line — including inside the value the attacker fully controls.

For example, running `git config core.hooksPath "/tmp/evil/repositoryformatversion"` in the attacker's own repository produces the config line:
```
	hooksPath = /tmp/evil/repositoryformatversion
```
This line contains the substring `repositoryformatversion`, so `filterGitConfig` keeps the line verbatim — including the real key `hooksPath` — even though `core.hooksPath` is explicitly not part of the documented allow-list (`core.repositoryFormatVersion` and `extensions.*`).

This function is invoked from `CreateSnapshot`, which is what the `GetSnapshot` RPC streams to callers: [2](#0-1) [3](#0-2) 

The snapshot's filtered `config` file is then consumed as trusted repository content by `CreateRepositoryFromSnapshot`, which extracts the archive directly on top of a freshly-initialized bare repository via `tar -xvf -`, overwriting the newly-init'd repo's own `config` file with whatever survived the filter: [4](#0-3) 

The code comment on that RPC already flags this as an area of concern ("NOTE: The received archive is trusted *a lot*..."), which supports that this is a legitimate trust boundary Gitaly is meant to enforce via the allow-list, and the substring bug defeats that intended enforcement.

### Impact Explanation
An attacker who owns a repository can inject an arbitrary `core.*` config line (as long as its text contains the substring `repositoryformatversion`) into the filtered snapshot config. If that snapshot is later consumed by `CreateRepositoryFromSnapshot` (used for repository moves/replication) or by any other downstream consumer (e.g., Geo replica, backup restore) that trusts the filtered config and re-applies it verbatim to a new repository, the injected `core.hooksPath` (or similar) becomes active in that new repository. Since Gitaly executes custom hooks found via `core.hooksPath`/`hooks/` on ref updates, this can lead to arbitrary command execution on the machine hosting the new repository once a push/receive triggers the hook — a real command/config-injection path from unprivileged, attacker-owned repository content into a system that is supposed to receive only a hardened, filtered configuration.

### Likelihood Explanation
Precondition is minimal: an ordinary unprivileged user needs only push/write access to a repository they own to set arbitrary `git config` values (a fully legitimate, non-privileged local operation). Triggering `GetSnapshot` (directly, or transitively through repository move/replication/Geo/backup flows that call `CreateSnapshot`) is enough to produce the poisoned config blob. Exploitation requires that some downstream consumer actually applies the snapshot's `config` file to a live repository without further validation — this is true of `CreateRepositoryFromSnapshot`'s tar-extraction-over-init flow within this codebase. The attack is fully repeatable and deterministic since the attacker fully controls the config value used to satisfy the substring check.

### Recommendation
Replace the substring "contains" check with exact, case-insensitive key-name matching. Parse each `core` section line into `key = value` (trimming and lower-casing only the key portion) and compare the key against the literal string `repositoryformatversion`, rejecting any line where the key doesn't match exactly, regardless of what appears in the value. Consider using a proper INI/git-config parser instead of manual line-based text matching to avoid similar section/continuation-line parsing edge cases.

### Proof of Concept
```go
func TestFilterGitConfig_SubstringBypass(t *testing.T) {
	data := []byte(`[core]
	repositoryformatversion = 1
	hooksPath = /tmp/evil/repositoryformatversion
	fsmonitor = /tmp/attacker_repositoryformatversion_script.sh
[extensions]
	worktreeConfig = true
`)

	filtered, err := filterGitConfig(data)
	require.NoError(t, err)

	// Only core.repositoryformatversion and extensions.* should survive.
	require.NotContains(t, string(filtered), "hooksPath")
	require.NotContains(t, string(filtered), "fsmonitor")
	require.Contains(t, string(filtered), "repositoryformatversion = 1")
	require.Contains(t, string(filtered), "worktreeConfig = true")
}
```
With the current implementation, this test fails: `hooksPath` and `fsmonitor` lines are retained in `filtered` because their values contain the substring `repositoryformatversion`, demonstrating that the allow-list filter can be bypassed with attacker-chosen config values in the attacker's own repository.

### Citations

**File:** internal/git/localrepo/snapshot.go (L64-69)
```go
	// References
	_ = builder.FileIfExist("HEAD")
	_ = builder.FileIfExist("packed-refs")
	// Only include core.repositoryFormatVersion and extensions.* from the config file
	_ = builder.FileWithEdit("config", false, filterGitConfig)
	_ = builder.RecursiveDirIfExist("refs")
```

**File:** internal/git/localrepo/snapshot.go (L114-119)
```go
		switch currentSection {
		case "core":
			// Only include repositoryformatversion from [core].
			if bytes.HasPrefix(trimmed, []byte("[")) || bytes.Contains(bytes.ToLower(trimmed), []byte("repositoryformatversion")) {
				result = append(result, line)
			}
```

**File:** internal/gitaly/service/repository/snapshot.go (L12-30)
```go
func (s *server) GetSnapshot(in *gitalypb.GetSnapshotRequest, stream gitalypb.RepositoryService_GetSnapshotServer) error {
	if err := s.locator.ValidateRepository(stream.Context(), in.GetRepository()); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	writer := streamio.NewWriter(func(p []byte) error {
		return stream.Send(&gitalypb.GetSnapshotResponse{Data: p})
	})

	err := s.localRepoFactory.Build(in.GetRepository()).CreateSnapshot(stream.Context(), writer)
	switch {
	case errors.Is(err, localrepo.ErrSnapshotAlternates):
		// This RPC historically does not consider an invalid alternates as a hard failure.
		s.logger.WithField("error", err).WarnContext(stream.Context(), "error getting alternate object directories")
	case err != nil:
		return err
	}

	return nil
```

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L115-144)
```go
	cmd, err := command.New(ctx, s.logger, []string{"tar", "-C", path, "-xvf", "-"}, command.WithStdin(rsp.Body))
	if err != nil {
		return err
	}

	return cmd.Wait()
}

func (s *server) CreateRepositoryFromSnapshot(ctx context.Context, in *gitalypb.CreateRepositoryFromSnapshotRequest) (*gitalypb.CreateRepositoryFromSnapshotResponse, error) {
	repository := in.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository, storage.WithSkipRepositoryExistenceCheck()); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}

	if err := repoutil.Create(ctx, s.logger, s.locator, s.gitCmdFactory, s.catfileCache, s.txManager, s.repositoryCounter, repository, func(repo *gitalypb.Repository) error {
		path, err := s.locator.GetRepoPath(ctx, repo, storage.WithRepositoryVerificationSkipped())
		if err != nil {
			return structerr.NewInternal("getting repo path: %w", err)
		}

		// The archive contains a partial git repository, missing a config file and
		// other important items. Initializing a new bare one and extracting the
		// archive on top of it ensures the created git repository has everything
		// it needs (especially, the config file and hooks directory).
		//
		// NOTE: The received archive is trusted *a lot*. Before pointing this RPC
		// at endpoints not under our control, it should undergo a lot of hardening.
		if err := s.untar(ctx, path, in); err != nil {
			return structerr.NewInternal("extracting snapshot: %w", err)
		}
```
