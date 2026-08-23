This confirms the "bundle URI" mechanism: `uploadpack.advertiseBundleURIs`/`bundle.<id>.uri` config lets a Git *server* advertise a URI (which the client's `git-fetch`/`git-clone` may retrieve) during a protocol negotiation with a *remote*. Gitaly explicitly disables this (`transfer.bundleURI=false`) in `cloneFromURLCommand` (used by `CreateRepositoryFromURL`) specifically because that RPC clones from an attacker/user-supplied remote URL, and a malicious remote could advertise a `bundle.<id>.uri` pointing at an internal address, causing Gitaly's `git-clone` to fetch it — an SSRF. This was fixed via the "repoFromURL: Disable bundle URIs when cloning from URL to prevent SSRF" security patch noted in `CHANGELOG.md`. [1](#0-0) 

The sibling function, `Repo.CloneBundle`, does not set this same guard. It builds an equivalent `git clone --mirror <bundlePath> <repoPath>` invocation, but only disables `transfer.fsckObjects`, never `transfer.bundleURI`: [2](#0-1) 

`CloneBundle` is invoked by the `CreateRepositoryFromBundle` RPC, which is reachable by any ordinary client that can call `RepositoryService`, streaming an arbitrary, attacker-crafted Git bundle: [3](#0-2) 

A crafted bundle can itself declare an embedded remote-tracking/`.bundle` capability line (Git's v3 bundle format supports a `@object-format=` and other capability lines, and — analogous to the remote case — Git's client-side clone/fetch machinery reads `bundle.<id>.uri`-style config coming from the source it is fetching from) which, combined with `transfer.bundleURI` remaining enabled (the Gitaly-wide default, since only `CreateRepositoryFromURL` explicitly disables it), would let Git follow a URI supplied inside the untrusted bundle and issue an outbound request from the Gitaly host to an address of the attacker's choosing (SSRF), or leak local network/service state through response handling. This mirrors the reported bug class exactly: one sibling entry point (`onERC1155Received`) enforces the security check while the structurally identical sibling (`onERC1155BatchReceived`) omits it — here, `CreateRepositoryFromURL`'s clone path disables `transfer.bundleURI` while the parallel `CreateRepositoryFromBundle`/`CloneBundle` clone path does not.

Because I could not obtain the actual upstream commit diff (the `gh_get_commit` lookup for `gitlab-org/gitaly` failed since I only have access to `Loderfordw/gitaly--019`), I cannot conclusively confirm from the fix's own description whether the bundle-file clone path was assessed as in-scope or already believed to be safe by the Git bundle-uri protocol; my finding is based on direct code comparison, not on the original security advisory text.

### Title
Missing `transfer.bundleURI` guard in `CreateRepositoryFromBundle`/`CloneBundle` allows SSRF via attacker-supplied bundle - (File: internal/git/localrepo/bundle.go)

### Summary
`CreateRepositoryFromURL` disables Git's `transfer.bundleURI` config before invoking `git clone` on an untrusted remote URL to prevent the remote from directing Gitaly to fetch an internal/arbitrary URI (SSRF). The parallel, equally-untrusted code path — `CreateRepositoryFromBundle`, which calls `Repo.CloneBundle` to `git clone --mirror` an attacker-supplied bundle stream — never applies this same guard.

### Finding Description
`cloneFromURLCommand` explicitly appends `gitcmd.WithGlobalOption(gitcmd.ConfigPair{Key: "transfer.bundleURI", Value: "false"})` before running `git clone`, with an inline comment stating this exists specifically "to prevent attack vectors that could abuse this mechanism" because the source can be any server, including localhost. [1](#0-0) 

`Repo.CloneBundle` performs the structurally identical operation — spawning `git clone --mirror <bundlePath> <repoPath>` — from data supplied wholesale by the RPC caller, but only disables `transfer.fsckObjects`; it never disables `transfer.bundleURI`. [2](#0-1) 

`CreateRepositoryFromBundle` streams the raw bundle bytes directly from the gRPC client into `CloneBundle` with no bundle-content validation beyond what `git clone` itself performs. [3](#0-2) 

Both call sites end up executing the same `git clone` machinery against untrusted, caller-controlled input (a remote URL in one case, a bundle blob in the other), but only one of the two sibling code paths carries the SSRF mitigation.

### Impact Explanation
If `git clone`'s bundle-transport handling honors bundle-uri-style capabilities/config embedded in or derived from the source being cloned (the same mechanism `transfer.bundleURI=false` was added to block for URL-based clones), an attacker who can call `CreateRepositoryFromBundle` with a crafted bundle could cause the Gitaly server process to issue an outbound HTTP(S) request to an arbitrary address of the attacker's choosing — the classic bundle-URI SSRF scenario, but reached through the bundle-import path instead of the remote-URL path.

### Likelihood Explanation
`CreateRepositoryFromBundle` is a standard, unprivileged `RepositoryService` RPC that any client authorized to create repositories can call with fully attacker-controlled bundle bytes, so the reachability is straightforward — no special network position or privileged role is required, matching how `CreateRepositoryFromURL` was reachable before its fix.

### Recommendation
Apply the same mitigation used in `cloneFromURLCommand` to `Repo.CloneBundle`: pass `gitcmd.WithGlobalOption(gitcmd.ConfigPair{Key: "transfer.bundleURI", Value: "false"})` (or equivalent) to the `git clone --mirror` invocation inside `CloneBundle`, so that bundle-uri-derived fetches are disabled regardless of whether the untrusted source is a remote URL or a bundle stream.

### Proof of Concept
Not independently verified against a live Git binary in this session; the analog is derived from direct comparison of `cloneFromURLCommand` (guarded) versus `CloneBundle` (unguarded), both of which invoke `git clone` against fully attacker/caller-controlled input via the respective `CreateRepositoryFromURL` and `CreateRepositoryFromBundle` RPCs. Confirming actual exploitability would require testing whether `git clone <bundle-file>` (bundle transport) evaluates `bundle.<id>.uri`/`transfer.bundleURI` config the same way the URL/remote transport does — this could not be validated with the tools available in this session and should be checked directly against the vendored Git version before treating this as confirmed exploitable versus defense-in-depth.

### Citations

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L72-84)
```go
	// Drop support for bundle URI when fetching from a remote repository.
	// Since the URI can point to any server, including localhost, this is to
	// prevent attack vectors that could abuse this mechanism.
	opts = append(opts, gitcmd.WithGlobalOption(gitcmd.ConfigPair{Key: "transfer.bundleURI", Value: "false"}))

	return s.gitCmdFactory.NewWithoutRepo(ctx,
		gitcmd.Command{
			Name:  "clone",
			Flags: cloneFlags,
			Args:  []string{urlString, repositoryFullPath},
		},
		append(opts, gitcmd.WithConfigEnv(config...))...,
	)
```

**File:** internal/git/localrepo/bundle.go (L91-113)
```go
	var cloneErr bytes.Buffer
	cloneCmd, err := repo.gitCmdFactory.NewWithoutRepo(ctx,
		gitcmd.Command{
			Name: "clone",
			Flags: []gitcmd.Option{
				gitcmd.Flag{Name: "--quiet"},
				gitcmd.Flag{Name: "--mirror"},
			},
			Args: []string{bundlePath, repoPath},
		},
		gitcmd.WithStderr(&cloneErr),
		gitcmd.WithDisabledHooks(),
		// Starting in Git version 2.46.0, executing git-fetch(1) on a bundle performs fsck
		// checks when `transfer.fsckObjects` is enabled. Prior to this, this configuration was
		// always ignored and fsck checks were not run. Unfortunately, fsck message severity
		// configuration is ignored by Git only for bundle fetches. Until this is supported by
		// Git, disable `transfer.fsckObjects` so bundles containing fsck errors can continue to
		// be fetched. This matches behavior prior to Git version 2.46.0.
		gitcmd.WithConfig(gitcmd.ConfigPair{Key: "transfer.fsckObjects", Value: "false"}),
	)
	if err != nil {
		return fmt.Errorf("spawning git-clone: %w", err)
	}
```

**File:** internal/gitaly/service/repository/create_repository_from_bundle.go (L13-45)
```go
func (s *server) CreateRepositoryFromBundle(stream gitalypb.RepositoryService_CreateRepositoryFromBundleServer) error {
	ctx := stream.Context()

	firstRequest, err := stream.Recv()
	if err != nil {
		return structerr.NewInternal("first request failed: %w", err)
	}

	repo := firstRequest.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repo, storage.WithSkipRepositoryExistenceCheck()); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	firstRead := false
	bundleReader := streamio.NewReader(func() ([]byte, error) {
		if !firstRead {
			firstRead = true
			return firstRequest.GetData(), nil
		}

		request, err := stream.Recv()
		return request.GetData(), err
	})

	if err := repoutil.Create(ctx, s.logger, s.locator, s.gitCmdFactory, s.catfileCache, s.txManager, s.repositoryCounter, repo, func(repo *gitalypb.Repository) error {
		if err := s.localRepoFactory.Build(repo).CloneBundle(ctx, bundleReader); err != nil {
			return structerr.NewInternal("cloning bundle: %w", err)
		}

		return nil
	}, repoutil.WithSkipInit()); err != nil {
		return structerr.NewInternal("creating repository: %w", err)
	}
```
