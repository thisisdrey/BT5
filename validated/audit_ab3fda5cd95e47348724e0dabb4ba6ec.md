### Title
Cross-repository state overwrite via mismatched `source`/`target` in `ReplicateRepository` - (File: internal/gitaly/service/repository/replicate.go)

### Summary
`ReplicateRepository` never verifies that the `source` and `target` repositories it is asked to synchronize refer to the same logical repository. It only checks that they live on *different* storages. Any caller who can invoke this RPC can therefore point an arbitrary, already-populated `target` repository at an unrelated `source` repository and have the target's Git config, references/objects, and custom hooks unconditionally overwritten with the source's content — the same "copy entire state from A onto B without an ownership/identity check" pattern described in the external report, just applied to repository state instead of vault-share holder state.

### Finding Description
`validateReplicateRepository` only enforces two things: that `target` passes basic repository-request validation, and that `source`/`target` are on different storages. [1](#0-0) 

There is no check that `source.RelativePath == target.RelativePath`. `ReplicateRepository` then does: [2](#0-1) 

If `target` already exists and is a valid repository, the creation branch (`s.create`) is skipped entirely and `s.replicateRepository` is invoked directly against the **existing** target repository using whatever `source` the caller supplied. `replicateRepository` then unconditionally overwrites the target's git config, references, and custom hooks with the source's: [3](#0-2) 

`syncCustomHooks` fetches the (attacker-controlled) source's custom hooks tar and installs it straight onto the target via `repoutil.SetCustomHooks`: [4](#0-3) 

`syncGitconfig` similarly overwrites the target's `config` file with the source's content: [5](#0-4) 

This mirrors the reported bug class exactly: an operation intended to synchronize/transfer state between two entities blindly copies the entire state object (here: git config + refs/objects + custom hooks) from the caller-specified source onto an existing, unrelated target, because the code never validates that source and target represent the same identity (relative path) — analogous to the missing check that "to" and "from" accounts must be the transfer counterparties for the same position.

### Impact Explanation
- **Custom hooks overwrite = arbitrary hook injection**: since `syncCustomHooks` installs the source's `custom_hooks` directory onto the target unconditionally, an attacker who controls a source repository can plant custom hooks (e.g. `pre-receive`/`post-receive`) into a victim's target repository. Those hooks will subsequently execute with the Gitaly host's privileges on any future push/change to the victim repository.
- **Data loss / cross-repository object access**: the victim's git config and reference set are silently overwritten (`config`, `refs/*`, `HEAD`) with the attacker's content, and the attacker's objects become reachable in the victim's repository — a concrete "cross-repository object access" outcome.
- This is a MUTATOR RPC on `RepositoryService`, reachable by any caller authorized to invoke Gitaly RPCs (e.g. via crafted request fields sent through Rails/Workhorse/Praefect or any client holding the shared Gitaly auth token used for ordinary RPC traffic), not requiring elevated internal privileges beyond what's needed for normal repository RPCs.

### Likelihood Explanation
The only gate is `source.GetStorageName() != target.GetStorageName()`, which any multi-storage Gitaly deployment satisfies trivially. There is no ownership/identity linkage check between `source` and `target`, so the exploit only requires crafting a `ReplicateRepositoryRequest` with an attacker-controlled `source` and a chosen `target` (which the attacker must be able to reference, e.g. via Praefect routing or knowledge of its relative path) — a straightforward crafted-RPC-field attack, not one requiring privileged internal access or a compromised peer.

### Recommendation
Enforce that `source.RelativePath == target.RelativePath` (or otherwise verify the two repositories are recognized as replicas of the same logical repository, e.g. via a validated repository-ID lookup) inside `validateReplicateRepository` before any config/hooks/reference synchronization is performed. Reject the request with an invalid-argument error if the identities don't match, closing the path for cross-repository state overwrite.

### Proof of Concept
1. Attacker has push/write access to `repo-attacker` on `storage-a`, containing malicious custom hooks and refs.
2. Attacker (or any client able to invoke `RepositoryService`) sends:
```
ReplicateRepository(
  source = {storage_name: "storage-a", relative_path: "repo-attacker.git"},
  repository = {storage_name: "storage-b", relative_path: "victim-repo.git"} // pre-existing, unrelated repo
)
```
3. Because `validateReplicateRepository` (internal/gitaly/service/repository/replicate.go:164-178) only checks storages differ, the request passes validation.
4. Since `victim-repo.git` already exists and is valid, `s.create` is skipped and `s.replicateRepository` (lines 127-162) runs directly, overwriting `victim-repo.git`'s config, refs/objects, and custom hooks with the attacker's `repo-attacker.git` content — including planting the attacker's `custom_hooks` on the victim repository.

### Citations

**File:** internal/gitaly/service/repository/replicate.go (L101-122)
```go
	if err := s.locator.ValidateRepository(ctx, in.GetRepository()); err != nil {
		repoPath, err := s.locator.GetRepoPath(ctx, in.GetRepository(), storage.WithRepositoryVerificationSkipped())
		if err != nil {
			return nil, structerr.NewInternal("%w", err)
		}

		if err = s.create(ctx, in, sourceBackend, repoPath); err != nil {
			if errors.Is(err, ErrInvalidSourceRepository) {
				return nil, ErrInvalidSourceRepository
			}

			return nil, structerr.NewInternal("%w", err)
		}
	}

	// The partitioning hint should not be forwarded to other Gitaly nodes as the path is irrelevant for them.
	outgoingCtx := storage.ContextWithoutPartitioningHint(ctx)
	outgoingCtx = metadata.IncomingToOutgoing(outgoingCtx)

	if err := s.replicateRepository(outgoingCtx, in.GetSource(), in.GetRepository()); err != nil {
		return nil, structerr.NewInternal("replicating repository: %w", err)
	}
```

**File:** internal/gitaly/service/repository/replicate.go (L127-162)
```go
func (s *server) replicateRepository(ctx context.Context, source, target *gitalypb.Repository) error {
	if err := s.syncGitconfig(ctx, source, target, func(ctx context.Context, path string, content io.Reader) error {
		if err := s.writeFile(ctx, path, content); err != nil {
			return err
		}

		if tx := storage.ExtractTransaction(ctx); tx != nil {
			originalConfigRelativePath, err := filepath.Rel(tx.FS().Root(), path)
			if err != nil {
				return fmt.Errorf("original config relative path: %w", err)
			}

			if err := tx.FS().RecordRemoval(originalConfigRelativePath); err != nil {
				return fmt.Errorf("record old config removal: %w", err)
			}

			if err := tx.FS().RecordFile(originalConfigRelativePath); err != nil {
				return fmt.Errorf("record new config creation: %w", err)
			}
		}

		return nil
	}); err != nil {
		return fmt.Errorf("synchronizing gitconfig: %w", err)
	}

	if err := s.syncReferences(ctx, source, target); err != nil {
		return fmt.Errorf("synchronizing references: %w", err)
	}

	if err := s.syncCustomHooks(ctx, source, target); err != nil {
		return fmt.Errorf("synchronizing custom hooks: %w", err)
	}

	return nil
}
```

**File:** internal/gitaly/service/repository/replicate.go (L164-178)
```go
func validateReplicateRepository(ctx context.Context, locator storage.Locator, in *gitalypb.ReplicateRepositoryRequest) error {
	if err := locator.ValidateRepository(ctx, in.GetRepository(), storage.WithSkipRepositoryExistenceCheck()); err != nil {
		return err
	}

	if in.GetSource() == nil {
		return errors.New("source repository cannot be empty")
	}

	if in.GetRepository().GetStorageName() == in.GetSource().GetStorageName() {
		return errors.New("repository and source have the same storage")
	}

	return nil
}
```

**File:** internal/gitaly/service/repository/replicate.go (L566-590)
```go
// syncCustomHooks replicates custom hooks from a source to a target.
func (s *server) syncCustomHooks(ctx context.Context, source, target *gitalypb.Repository) error {
	repoClient, err := s.newRepoClient(ctx, source.GetStorageName())
	if err != nil {
		return fmt.Errorf("creating repo client: %w", err)
	}

	stream, err := repoClient.GetCustomHooks(ctx, &gitalypb.GetCustomHooksRequest{
		Repository: source,
	})
	if err != nil {
		return fmt.Errorf("getting custom hooks: %w", err)
	}

	reader := streamio.NewReader(func() ([]byte, error) {
		request, err := stream.Recv()
		return request.GetData(), err
	})

	if err := repoutil.SetCustomHooks(ctx, s.logger, s.locator, s.txManager, reader, target); err != nil {
		return fmt.Errorf("setting custom hooks: %w", err)
	}

	return nil
}
```

**File:** internal/gitaly/service/repository/replicate.go (L592-615)
```go
func (s *server) syncGitconfig(ctx context.Context, source, target *gitalypb.Repository, writeConfig func(ctx context.Context, path string, content io.Reader) error) error {
	repoClient, err := s.newRepoClient(ctx, source.GetStorageName())
	if err != nil {
		return err
	}

	repoPath, err := s.locator.GetRepoPath(ctx, target)
	if err != nil {
		return err
	}

	stream, err := repoClient.GetConfig(ctx, &gitalypb.GetConfigRequest{
		Repository: source,
	})
	if err != nil {
		return err
	}

	configPath := filepath.Join(repoPath, "config")
	return writeConfig(ctx, configPath, streamio.NewReader(func() ([]byte, error) {
		resp, err := stream.Recv()
		return resp.GetData(), err
	}))
}
```
