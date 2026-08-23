### Title
Repository-creation RPCs (`CreateFork`, `CreateRepositoryFromURL`, `CreateRepositoryFromBundle`, `CreateObjectPool`) force an unavoidable, size-unbounded clone plus full repack on the RPC hot path, letting an ordinary fork/import action impose disproportionate CPU/IO cost on Gitaly - ([File: internal/gitaly/repoutil/create.go])

### Summary
The Timeswap report describes a case where a supposedly cheap user action (`mint`) implicitly triggers an extremely expensive, unavoidable side effect (full contract deployment) that the caller cannot opt out of. The Gitaly analog is `repoutil.Create`, the shared helper backing every repository-creation RPC (`CreateFork`, `CreateRepositoryFromURL`, `CreateRepositoryFromBundle`, `CreateObjectPool`, `CreateRepository`). Whenever the call runs inside a WAL transaction, `Create` unconditionally performs a full repack of the freshly created repository before it can be committed, with no opt-out and no bound on the size of the repository being repacked [1](#0-0) . For `CreateFork` and `CreateRepositoryFromURL`, this forced repack is chained directly after a synchronous `git clone`/`git fetch` of caller-controlled content (an existing GitLab repository for forks, or an arbitrary external URL for imports) that itself has no size or time limit enforced by Gitaly [2](#0-1) [3](#0-2) .

### Finding Description
`repoutil.Create` is the single implementation shared by every "create a new repository" RPC in Gitaly. It creates the repository in a temporary directory, invokes the caller-supplied `seedRepository` callback (which performs the actual `git clone`/`git fetch`/bundle-extraction work), and — if a WAL transaction is active — forces a full repack of the resulting repository before recording it and voting on the transaction: [1](#0-0) 

The comment explicitly acknowledges this is done unconditionally "to drop all unreachable objects" and ensure WAL invariants hold, regardless of how large the freshly seeded repository is. There is no `CreateOption` to skip this and no size ceiling.

Both `CreateFork` and `CreateRepositoryFromURL` are RPCs that ordinary, unprivileged users can trigger through everyday product actions (forking a project, importing a project by URL). Each performs a full, synchronous `git clone`/`git fetch` of content chosen by the calling user (the source repository being forked, or an arbitrary external URL for import) with no per-request size or time bound at the Gitaly layer:
- `CreateFork` fetches the entire source repository via `git clone --bare` over the internal sidechannel [2](#0-1) .
- `CreateRepositoryFromURL` clones from a user-supplied URL with `git clone --bare`/`--mirror` [4](#0-3) .

After either of these expensive clones completes, `repoutil.Create` immediately forces a *second* expensive operation — a full `git-repack` — on the same repository, still within the same RPC call and still before the transaction can commit, with no option for the caller (or Gitaly's own RPC layer) to defer, skip, or bound this cost. This mirrors the Timeswap pattern precisely: an outwardly "normal" user action (`mint` / "fork a project", "import a project") silently triggers a second, much more expensive, unavoidable operation ("deploy a full pair contract" / "full repack a full clone") as an undocumented, non-optional side effect.

### Impact Explanation
An attacker (or simply a user forking/importing an unusually large upstream repository) can force Gitaly to perform a full clone plus full repack synchronously inside a single RPC handler, with no built-in limit on repository size or wall-clock duration. Because this happens per-request and is baked into the shared `repoutil.Create` path used by all repository-creation RPCs, repeated fork/import requests against large or adversarially-constructed repositories can consume disproportionate CPU, disk I/O, and hold the target partition's single-writer `TransactionManager` busy, degrading or denying service for other tenants sharing that partition/storage. This matches the "DoS of a handler" acceptance criterion.

### Likelihood Explanation
`CreateFork` and `CreateRepositoryFromURL` are reachable by ordinary, unprivileged users through standard product flows (forking any project one can read, or importing a project by URL). Triggering the expensive path requires no special access — only supplying or selecting a large source repository. Since Gitaly's `repoutil.Create` performs this work unconditionally whenever transactions are enabled, the exposure is systemic rather than edge-case.

### Recommendation
- Make the post-creation full repack conditional/bounded: skip it (or defer it to background housekeeping) when the seeded repository is known to already be well-formed (e.g., freshly cloned via `git clone`, which already guarantees connectivity), instead of unconditionally repacking every newly created repository inside the synchronous RPC path.
- Enforce a configurable maximum repository size / object count and a request timeout for the underlying `git clone`/`git fetch` invoked by `CreateFork` and `CreateRepositoryFromURL` before allowing the subsequent repack to run.
- Consider moving the mandatory repack out of the critical RPC path into an asynchronous, rate-limited housekeeping job so a single fork/import request cannot monopolize partition resources.

### Proof of Concept
1. As an unprivileged user with fork/import permission, call `CreateRepositoryFromURL` (or `CreateFork`) pointing at a very large, adversarially crafted Git repository (e.g., many large blobs/loose objects) reachable over HTTP.
2. Observe that Gitaly performs the full `git clone` synchronously in `internal/gitaly/service/repository/create_repository_from_url.go` `CreateRepositoryFromURL`, then — because `repoutil.Create` runs under a WAL transaction — immediately performs an additional full `git-repack` of the entire cloned repository in `performFullRepack` before it can commit [1](#0-0) .
3. Repeating this call concurrently against several such repositories on the same storage/partition demonstrates sustained CPU/IO consumption and serialization on the partition's `TransactionManager`, with no Gitaly-side control to cap the cost.

### Citations

**File:** internal/gitaly/repoutil/create.go (L247-263)
```go
	if tx := storage.ExtractTransaction(ctx); tx != nil {
		// Git allows writing unreachable objects into the repository that are missing their dependencies. The reachable
		// ones are checked through connectivity checks but unreachable ones are not.
		//
		// Transactions rely on a property that all objects in the repository have all of their dependencies met. This allows
		// us to skip full connectivity checks, and simply check that the immediate dependencies of the newly written objects
		// are satisfied. Repository creations are used in various contexts and not all of them guarantee this property. Perform
		// a full repack to drop all unreachable objects. This way we're certain all of the objects committed through a repository
		// creation have their dependencies satisified. Ideally we would only perform a connectivity check of the new objects,
		// and record the dependencies that must exist in the repository already. Repository creations should generally include
		// all objects so the rewriting should not be needed. Issue: https://gitlab.com/gitlab-org/gitaly/-/issues/5969
		if err := performFullRepack(ctx, localrepo.New(logger, locator, gitCmdFactory, catfileCache, &gitalypb.Repository{
			StorageName:  repository.GetStorageName(),
			RelativePath: repository.GetRelativePath(),
		})); err != nil {
			return fmt.Errorf("perform full repack: %w", err)
		}
```

**File:** internal/gitaly/service/repository/create_fork.go (L64-92)
```go
		cmd, err := s.gitCmdFactory.NewWithoutRepo(ctx,
			gitcmd.Command{
				Name:  "clone",
				Flags: flags,
				Args: []string{
					gitcmd.InternalGitalyURL,
					targetPath,
				},
			},
			gitcmd.WithInternalFetchWithSidechannel(&gitalypb.SSHUploadPackWithSidechannelRequest{
				Repository: sourceRepository,
			}),
			gitcmd.WithConfig(gitcmd.ConfigPair{
				// Disable consistency checks for fetched objects when creating a
				// fork. We don't want to end up in a situation where it's
				// impossible to create forks we already have anyway because we have
				// e.g. retroactively tightened the consistency checks.
				Key: "fetch.fsckObjects", Value: "false",
			}),
			gitcmd.WithDisabledHooks(),
			gitcmd.WithStderr(&stderr),
		)
		if err != nil {
			return fmt.Errorf("spawning fetch: %w", err)
		}

		if err := cmd.Wait(); err != nil {
			return fmt.Errorf("fetching source repo: %w, stderr: %q", err, stderr.String())
		}
```

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L21-85)
```go
func (s *server) cloneFromURLCommand(
	ctx context.Context,
	repoURL, resolvedAddress, repositoryFullPath, authorizationToken string, mirror bool,
	opts ...gitcmd.CmdOpt,
) (*command.Command, error) {
	cloneFlags := []gitcmd.Option{
		gitcmd.Flag{Name: "--quiet"},
	}

	if mirror {
		cloneFlags = append(cloneFlags, gitcmd.Flag{Name: "--mirror"})
	} else {
		cloneFlags = append(cloneFlags, gitcmd.Flag{Name: "--bare"})
	}

	u, err := url.Parse(repoURL)
	if err != nil {
		return nil, structerr.NewInternal("%w", err)
	}

	var config []gitcmd.ConfigPair
	if u.User != nil {
		password, hasPassword := u.User.Password()

		var creds string
		if hasPassword {
			creds = u.User.Username() + ":" + password
		} else {
			creds = u.User.Username()
		}

		u.User = nil
		authHeader := fmt.Sprintf("Authorization: Basic %s", base64.StdEncoding.EncodeToString([]byte(creds)))
		config = append(config, gitcmd.ConfigPair{Key: "http.extraHeader", Value: authHeader})
	} else if len(authorizationToken) > 0 {
		authHeader := fmt.Sprintf("Authorization: %s", authorizationToken)
		config = append(config, gitcmd.ConfigPair{Key: "http.extraHeader", Value: authHeader})
	}

	urlString := u.String()

	if resolvedAddress != "" {
		modifiedURL, resolveConfig, err := gitcmd.GetURLAndResolveConfig(u.String(), resolvedAddress)
		if err != nil {
			return nil, structerr.NewInvalidArgument("couldn't get curloptResolve config: %w", err)
		}

		urlString = modifiedURL
		config = append(config, resolveConfig...)
	}

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
}
```

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L87-135)
```go
func (s *server) CreateRepositoryFromURL(ctx context.Context, req *gitalypb.CreateRepositoryFromURLRequest) (*gitalypb.CreateRepositoryFromURLResponse, error) {
	if err := validateCreateRepositoryFromURLRequest(ctx, s.locator, req); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}

	if err := repoutil.Create(ctx, s.logger, s.locator, s.gitCmdFactory, s.catfileCache, s.txManager, s.repositoryCounter, req.GetRepository(), func(repoProto *gitalypb.Repository) error {
		targetPath, err := s.locator.GetRepoPath(ctx, repoProto, storage.WithRepositoryVerificationSkipped())
		if err != nil {
			return fmt.Errorf("getting temporary repository path: %w", err)
		}

		var stderr bytes.Buffer
		cmd, err := s.cloneFromURLCommand(ctx,
			req.GetUrl(),
			req.GetResolvedAddress(),
			targetPath,
			req.GetHttpAuthorizationHeader(),
			req.GetMirror(),
			gitcmd.WithStderr(&stderr),
			gitcmd.WithDisabledHooks(),
		)
		if err != nil {
			return fmt.Errorf("starting clone: %w", err)
		}

		if err := cmd.Wait(); err != nil {
			stderrStr := stderr.String()
			if remoteNotFoundRegex.MatchString(stderrStr) {
				return structerr.NewNotFound("cloning repository: repository at given URL not found").
					WithDetail(&gitalypb.CreateRepositoryFromURLError{
						Error: &gitalypb.CreateRepositoryFromURLError_RemoteNotFound{},
					})
			}

			return structerr.NewInternal("cloning repository: %w, stderr: %q", err, stderrStr).WithMetadataItems(
				structerr.MetadataItem{Key: "stderr", Value: stderrStr},
				structerr.MetadataItem{Key: "resolved_address", Value: req.GetResolvedAddress()},
			)
		}

		repo := s.localRepoFactory.Build(repoProto)
		if err := s.removeOriginInRepo(ctx, repo); err != nil {
			return fmt.Errorf("removing origin remote: %w", err)
		}

		return nil
	}, repoutil.WithSkipInit()); err != nil {
		return nil, structerr.NewInternal("creating repository: %w", err)
	}
```
