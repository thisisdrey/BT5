### Title
`--alternate-refs` pseudo-revision leaks pooled-repository refs (including private upstream/dangling objects) because `core.alternateRefsCommand` is only neutralized for `fetch`/`receive-pack`, not for `rev-list` - ([File: internal/git/gitcmd/command_description.go])

### Summary
`internal/git/gitcmd/command_description.go`'s `"rev-list"` entry allows the pseudo-revision `--alternate-refs` through its `validatePositionalArgs` (via `git.ValidateRevision(..., git.AllowPseudoRevision())`), and unlike the `"fetch"` and `"receive-pack"` entries it has no `opts` that set `core.alternateRefsCommand=exit 0 #`. Multiple read-only RPCs (`ListCommits`, `CountCommits`, `ObjectsSize`, `ListLFSPointers`) also accept attacker-supplied `revisions`/pseudo-revisions with `git.AllowPseudoRevision()` and forward them straight into `git-rev-list(1)`, so an attacker whose repository is linked (via `objects/info/alternates`) to an object pool can pass `--alternate-refs` to walk and disclose commits/refs of the pool - including stale/private upstream refs and dangling objects the pool retains - that are not reachable from the attacker's own repository refs.

### Finding Description
`commandDescriptions["rev-list"]` explicitly documents that it must permit pseudo-revisions like `--all`/`--not` and implements this via `validatePositionalArgs`, which calls `git.ValidateRevision([]byte(arg), git.AllowPseudoRevision())` for any dash-prefixed argument [1](#0-0) . `git.ValidateRevision` with `AllowPseudoRevision()` explicitly whitelists `--alternate-refs` as a valid "revision" [2](#0-1) .

Several read RPCs validate attacker-controlled revision fields with exactly this option and then execute `git-rev-list`/related walks with them: `ListCommits` [3](#0-2) , `CountCommits` [4](#0-3) , `ObjectsSize` (which builds a raw `rev-list --disk-usage --objects --stdin` command and streams the client-supplied revisions on stdin) [5](#0-4) , and `ListLFSPointers` [6](#0-5) .

Crucially, unlike `"fetch"` and `"receive-pack"`, which set `ConfigPair{Key: "core.alternateRefsCommand", Value: "exit 0 #"}` specifically to suppress Git's default behavior of advertising/including refs from the alternate object database when it walks alternates [7](#0-6) [8](#0-7) , the `"rev-list"` entry has `opts: nil` and applies no such override [9](#0-8) . Gitaly's own documentation confirms `core.alternateRefsCommand=exit 0 #` exists precisely to stop advertising alternate/pool references (including dangling ones and previously-deleted refs still retained in the pool) because by default an alternate's refs, including stale/private-upstream references, would otherwise be visible [10](#0-9) . Without this override, `git-rev-list --alternate-refs` executes `git-for-each-ref` (the Git default) against the pool's alternate object directory and walks every ref that exists there — including refs mirroring an upstream/source repository that has since been made private or had branches removed, and dangling refs Gitaly explicitly creates to keep force-pushed/deleted objects alive in the pool (see `doc/object_pools.md` "Dangling Objects") [11](#0-10) .

Attack precondition: the attacker's own repository (e.g., a fork they own) has an `objects/info/alternates` file pointing at an object pool, which is standard, default behavior for GitLab forks (see object-pool linking flow) [12](#0-11) . GitLab's own test fixtures confirm object pools frequently have a designated (possibly private) "upstream" member whose visibility must be hidden from other pool consumers [13](#0-12) , and dedicated code (`ListPoolUpstreams`) exists specifically to prevent leaking a private upstream's identity [14](#0-13) , showing GitLab treats pool-upstream privacy as a real security boundary that this `rev-list` path can bypass at the object/commit-content level.

### Impact Explanation
An attacker who owns a fork/repository linked to an object pool can request commit history/metadata (`ListCommits`, `CountCommits`), object sizes (`ObjectsSize`), or LFS pointer scans (`ListLFSPointers`) using the revision `--alternate-refs`. This causes Git to walk and return information about commits/objects that are part of the object pool but not reachable from any ref in the attacker's own repository, including: (a) refs of the private/upstream pool member as of the last `FetchIntoObjectPool` sync, and (b) dangling objects retained for force-pushed/deleted history across the whole pool (which may include commits from other, unrelated pool members). This is disclosure of another repository's objects/metadata across a privacy boundary that GitLab otherwise protects (per `ListPoolUpstreams`'s explicit privacy filtering). This maps to GitLab's bounty class of "cross-repository object/data disclosure" (unauthorized information disclosure of another project's content).

### Likelihood Explanation
Requires no privileged access: an ordinary user who owns a forked repository (default GitLab fork flow links the fork to an object pool) can issue this via a single RPC call with a crafted `revisions` field. No secrets, no misconfiguration beyond default object-pool behavior, and it is fully repeatable/scriptable. The bar to reach it is simply having a fork of a project that is (or was) a pool member with other pool members whose refs the attacker shouldn't see.

### Recommendation
Add the same `core.alternateRefsCommand=exit 0 #` (or equivalent) mitigation to the `"rev-list"` command description's `opts`, or strip/reject the `--alternate-refs` pseudo-revision in all RPC-facing revision validation paths that call `git.ValidateRevision(..., git.AllowPseudoRevision())` unless explicitly required and safe (e.g., internal housekeeping paths only). At minimum, `ListCommits`, `CountCommits`, `ObjectsSize`, and `ListLFSPointers` should not permit `--alternate-refs` from untrusted RPC input.

### Proof of Concept
```go
// internal/gitaly/service/commit/list_commits_test.go style PoC
func TestListCommits_AlternateRefsLeaksPoolObjects(t *testing.T) {
    ctx := testhelper.Context(t)
    cfg, client := setupCommitServiceWithRepo(t, ctx) // helper setting up service+client

    // 1. Create "upstream" repo with a secret commit, treat as pool's primary/private member.
    upstreamRepoProto, upstreamPath := gittest.CreateRepository(t, ctx, cfg, gittest.CreateRepositoryConfig{})
    secretCommit := gittest.WriteCommit(t, cfg, upstreamPath, gittest.WithBranch("secret-branch"))

    // 2. Create an object pool from upstream, and fetch upstream refs into it.
    pool, _, _ := createObjectPool(t, ctx, cfg, upstreamRepoProto)
    // FetchIntoObjectPool pulls +refs/*:refs/remotes/origin/* from upstream into the pool.

    // 3. Create attacker's fork and link ONLY the fork (not upstream visibility) to the pool.
    forkRepoProto, _ := gittest.CreateRepository(t, ctx, cfg, gittest.CreateRepositoryConfig{})
    objectPoolClient.LinkRepositoryToObjectPool(ctx, &gitalypb.LinkRepositoryToObjectPoolRequest{
        ObjectPool: pool, Repository: forkRepoProto,
    })
    // The fork has no ref pointing at secretCommit.

    // 4. Attacker calls ListCommits on their own fork with the pseudo-revision.
    stream, err := client.ListCommits(ctx, &gitalypb.ListCommitsRequest{
        Repository: forkRepoProto,
        Revisions:  []string{"--alternate-refs"},
    })
    require.NoError(t, err)
    commits := consumeListCommits(t, stream)

    // Expected (buggy) result: secretCommit from the private upstream/pool is returned,
    // even though the fork itself has no ref reaching it.
    require.Contains(t, commitIDs(commits), secretCommit.String())
}
```
Equivalent PoC can be constructed against `CountCommits` (`Revisions: []string{"--alternate-refs"}`), `ObjectsSize` (`Revisions: [][]byte{[]byte("--alternate-refs")}`), or `ListLFSPointers`, all of which pass `git.AllowPseudoRevision()` validation and forward the argument to `git-rev-list` without the `core.alternateRefsCommand` suppression applied to `fetch`/`receive-pack`.

### Citations

**File:** internal/git/gitcmd/command_description.go (L112-112)
```go
				ConfigPair{Key: "core.alternateRefsCommand", Value: "exit 0 #"},
```

**File:** internal/git/gitcmd/command_description.go (L282-282)
```go
				ConfigPair{Key: "core.alternateRefsCommand", Value: "exit 0 #"},
```

**File:** internal/git/gitcmd/command_description.go (L317-340)
```go
	"rev-list": {
		// We cannot use --end-of-options here because pseudo revisions like `--all`
		// and `--not` count as options.
		flags: scNoRefUpdates | scNoEndOfOptions,
		validatePositionalArgs: func(args []string) error {
			for _, arg := range args {
				// git-rev-list(1) supports pseudo-revision arguments which can be
				// intermingled with normal positional arguments. Given that these
				// pseudo-revisions have leading dashes, normal validation would
				// refuse them as positional arguments. We thus override validation
				// for two of these which we are using in our codebase.
				if strings.HasPrefix(arg, "-") {
					if err := git.ValidateRevision([]byte(arg), git.AllowPseudoRevision()); err != nil {
						return structerr.NewInvalidArgument(
							"validating positional argument: %w", err,
						).WithMetadata("argument", arg)
					}

					continue
				}
			}

			return nil
		},
```

**File:** internal/git/revision.go (L50-68)
```go
	if cfg.allowPseudoRevisions {
		switch {
		case bytes.Equal(revision, []byte("--all")):
			return nil
		case bytes.Equal(revision, []byte("--not")):
			return nil
		case bytes.Equal(revision, []byte("--branches")):
			return nil
		case bytes.Equal(revision, []byte("--tags")):
			return nil
		case bytes.Equal(revision, []byte("--alternate-refs")):
			return nil
		case bytes.HasPrefix(revision, []byte("--branches=")):
			return nil
		case bytes.HasPrefix(revision, []byte("--tags=")):
			return nil
		case bytes.HasPrefix(revision, []byte("--glob=")):
			return nil
		}
```

**File:** internal/gitaly/service/commit/list_commits.go (L62-73)
```go
func verifyListCommitsRequest(ctx context.Context, locator storage.Locator, request *gitalypb.ListCommitsRequest) error {
	if err := locator.ValidateRepository(ctx, request.GetRepository()); err != nil {
		return err
	}
	if len(request.GetRevisions()) == 0 {
		return errors.New("missing revisions")
	}
	for _, revision := range request.GetRevisions() {
		if err := git.ValidateRevision([]byte(revision), git.AllowPseudoRevision()); err != nil {
			return structerr.NewInvalidArgument("invalid revision: %w", err).WithMetadata("revision", revision)
		}
	}
```

**File:** internal/gitaly/service/commit/count_commits.go (L80-97)
```go
func validateCountCommitsRequest(ctx context.Context, locator storage.Locator, in *gitalypb.CountCommitsRequest) error {
	if err := locator.ValidateRepository(ctx, in.GetRepository()); err != nil {
		return err
	}

	if len(in.GetRevisions()) > 0 {
		for _, revision := range in.GetRevisions() {
			if err := git.ValidateRevision(revision, git.AllowPseudoRevision()); err != nil {
				return structerr.NewInvalidArgument("invalid revision: %w", err).WithMetadata("revision", string(revision))
			}
		}
		return nil
	}

	//nolint:staticcheck // Revision is deprecated in favor of revisions field
	if err := git.ValidateRevision(in.GetRevision(), git.AllowEmptyRevision()); err != nil {
		return err
	}
```

**File:** internal/gitaly/service/repository/objects_size.go (L38-73)
```go
	var stderr, stdout strings.Builder
	cmd, err := repo.Exec(ctx,
		gitcmd.Command{
			Name: "rev-list",
			Flags: []gitcmd.Option{
				gitcmd.Flag{Name: "--disk-usage"},
				gitcmd.Flag{Name: "--objects"},
				gitcmd.Flag{Name: "--stdin"},
			},
		},
		gitcmd.WithStderr(&stderr),
		gitcmd.WithStdout(&stdout),
		gitcmd.WithSetupStdin())
	if err != nil {
		return fmt.Errorf("start rev-list command: %w", err)
	}

	for i := 0; ; i++ {
		if i != 0 && request.GetRepository() != nil {
			return structerr.NewInvalidArgument("subsequent requests must not contain repository")
		}

		if len(request.GetRevisions()) == 0 {
			return structerr.NewInvalidArgument("no revisions specified")
		}

		for _, revision := range request.GetRevisions() {
			if err := git.ValidateRevision(revision, git.AllowPseudoRevision()); err != nil {
				return structerr.NewInvalidArgument("validating revision: %w", err).WithMetadata("revision", revision)
			}

			// Each revision must be separated by a newline when the `--stdin` option is used, as Git
			// parses these differently to command-line arguments.
			if _, err := cmd.Write([]byte(fmt.Sprintf("%s\n", revision))); err != nil {
				return structerr.NewInvalidArgument("process revision: %w", err).WithMetadata("revision", revision)
			}
```

**File:** internal/gitaly/service/blob/lfs_pointers.go (L38-42)
```go
	for _, revision := range in.GetRevisions() {
		if err := git.ValidateRevision([]byte(revision), git.AllowPathScopedRevision(), git.AllowPseudoRevision()); err != nil {
			return structerr.NewInvalidArgument("invalid revision: %w", err).WithMetadata("revision", revision)
		}
	}
```

**File:** doc/object_pools.md (L93-112)
```markdown
As a safeguard to not lose any objects by accident, we thus create dangling
references in the object pool after the fetch in `FetchIntoObjectPool`. For each
dangling object, a reference `refs/dangling/$OID` is created which points into
the object. This assures that each object is still referenced.

Having unreachable objects kept alive in this fashion does have its problems:

- For busy repositories, we generate loads of dangling references. While these
  references [cannot be seen by clients](#references), they are seen when
  performing housekeeping tasks on the object pool itself. Fetches into the
  object pool and repacking of references can thus become quite expensive.

- Keeping dangling references alive makes Git consider them as reachable. While
  this is the exact effect we want to achieve, it will also cause Git to
  generate packfiles which may use such objects as delta bases which would under
  normal circumstances be considered as unreachable. The resulting packfile is
  thus potentially suboptimal. Gitaly works around this issue by using a delta
  island for `refs/heads/` and `refs/tags/`. This can only be considered a
  best-effort strategy, as it only considers a single object pool member's
  reachability while ignoring potential reachability by any other pool member.
```

**File:** doc/object_pools.md (L114-125)
```markdown
## References

When Git repositories have alternates set up, then they by default advertise any
references of the alternate itself. A client would thus typically also see both
dangling references as well as any other reference which was potentially already
deleted in the pool member which the client is fetching from. Besides being
inefficient, the resulting references would also be wrong.

To avoid advertising of such references, Gitaly uses a workaround of setting the
config entry `core.alternateRefsCommand=exit 0 #`. This causes Git to use the
given command instead of executing `git-for-each-ref(1)` in the alternate and thus
stops it from advertising alternate references.
```

**File:** internal/git/objectpool/link.go (L19-27)
```go
// Link calls the non-receiver method version of Link with the parameters
// injected from the object pool.
func (o *ObjectPool) Link(ctx context.Context, repo *localrepo.Repo) error {
	return Link(ctx, o.Repo, repo, o.txManager)
}

// Link will link the given repository to the object pool. This is done by writing the object pool's
// path relative to the repository into the repository's "alternates" file. This does not trigger
// deduplication, which is the responsibility of the caller.
```

**File:** internal/cli/gitaly/subcmd_pool_test.go (L211-234)
```go
	t.Run("private upstream is not marked", func(t *testing.T) {
		t.Parallel()

		ctx := testhelper.Context(t)
		cfg := testcfg.Build(t)
		storageRoot := cfg.Storages[0].Path

		store := newStore(t)

		addr := testserver.RunGitalyServer(t, cfg, setup.RegisterAll,
			testserver.WithPoolMetadataStore(store),
			testserver.WithGitLabClient(gitlab.NewMockClientWithObjectPoolMembers(t,
				gitlab.MockAllowed, gitlab.MockPreReceive, gitlab.MockPostReceive,
				func(_ context.Context, diskPaths []string, _ string, _ bool) (map[string][]gitlab.ObjectPoolMember, error) {
					result := make(map[string][]gitlab.ObjectPoolMember, len(diskPaths))
					for _, diskPath := range diskPaths {
						result[diskPath] = []gitlab.ObjectPoolMember{{
							RelativePath: "private-upstream.git",
							Public:       false,
							IsUpstream:   true,
						}}
					}
					return result, nil
				},
```

**File:** internal/gitaly/service/internalgitaly/list_pool_upstreams_test.go (L134-173)
```go
	t.Run("pool with private upstream is omitted", func(t *testing.T) {
		cfg := testcfg.Build(t)
		storageName := cfg.Storages[0].Name

		srv := NewServer(&service.Dependencies{
			Logger:         testhelper.SharedLogger(t),
			Cfg:            cfg,
			StorageLocator: config.NewLocator(cfg),
			GitlabClient: gitlab.NewMockClientWithObjectPoolMembers(
				t,
				gitlab.MockAllowed,
				gitlab.MockPreReceive,
				gitlab.MockPostReceive,
				func(_ context.Context, diskPaths []string, _ string, _ bool) (map[string][]gitlab.ObjectPoolMember, error) {
					return map[string][]gitlab.ObjectPoolMember{
						diskPaths[0]: {
							{
								RelativePath: "private-repo.git",
								Public:       false,
								IsUpstream:   true,
							},
						},
					}, nil
				},
			),
		})
		client := setupInternalGitalyService(t, cfg, srv)

		stream, err := client.ListPoolUpstreams(ctx)
		require.NoError(t, err)
		require.NoError(t, stream.Send(&gitalypb.ListPoolUpstreamsRequest{
			StorageName:   storageName,
			PoolDiskPaths: []string{"@pools/aa/bb/private-pool.git"},
		}))
		require.NoError(t, stream.CloseSend())

		results := consumeServerStream(t, stream)
		require.Len(t, results, 1)
		require.Empty(t, results[0].GetUpstreams())
	})
```
