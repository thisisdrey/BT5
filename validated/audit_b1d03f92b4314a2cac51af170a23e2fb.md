### No vulnerability found for this question.

**Rationale:** The scenario described doesn't introduce a new authorization bypass. `objectpool.Disconnect` (internal/git/objectpool/disconnect.go:36-134) hard-links whatever objects currently exist in the alternate object directory via `findObjectFiles`/`storage.Link` [1](#0-0)  before ripping out the `objects/info/alternates` file and validating connectivity in `removeAlternatesIfOk` [2](#0-1) . While alternates are attached, a pool member already has full read access to the entire pool object directory — that's the fundamental semantics of git alternates, not something the TOCTOU window creates. Any object pushed into the pool by another still-linked member during the race window was already reachable by the attacker's repo through the alternate path at that same moment, exactly as it would be absent any concurrent `Disconnect` call. So the race doesn't let the attacker access objects they weren't already authorized to read as a pool member; it only affects whether those objects get materialized as hard links into the attacker's own private object store at disconnect time versus remaining reachable only while still pooled.

The other described risk — the repository being left in a temporarily broken/corrupted state if `Disconnect` races with a crash or fails connectivity mid-flight — is an explicitly documented, accepted operational risk in the code's own comments [3](#0-2)  and [4](#0-3) , mitigated by preserving a `backupFile` for administrator restoration [5](#0-4) . This only self-damages the caller's own repository (a resource they already control), which does not constitute cross-tenant impact or an authorization bypass under the stated rules.

### Citations

**File:** internal/git/objectpool/disconnect.go (L89-117)
```go
	objectFiles, err := findObjectFiles(altObjectDir)
	if err != nil {
		return err
	}

	repositoryRelativePath, err := filepath.Rel(f.Root(), repoPath)
	if err != nil {
		return fmt.Errorf("repository relative path: %w", err)
	}

	for _, path := range objectFiles {
		sourceRelativePath, err := filepath.Rel(f.Root(), filepath.Join(altObjectDir, path))
		if err != nil {
			return fmt.Errorf("source relative path: %w", err)
		}
		targetRelativePath := filepath.Join(repositoryRelativePath, "objects", path)

		if err := storage.MkdirAll(f, filepath.Dir(targetRelativePath)); err != nil {
			return err
		}

		if err := storage.Link(f, sourceRelativePath, targetRelativePath); err != nil {
			if errors.Is(err, fs.ErrExist) {
				continue
			}

			return err
		}
	}
```

**File:** internal/git/objectpool/disconnect.go (L203-212)
```go
// removeAlternatesIfOk is dangerous. We optimistically temporarily
// rename objects/info/alternates, and run `git fsck` to see if the
// resulting repo is connected. If this fails we restore
// objects/info/alternates. If the repo is not connected for whatever
// reason, then until this function returns, probably **all concurrent
// RPC calls to the repo will fail**. Also, if Gitaly crashes in the
// middle of this function, the repo is left in a broken state. We do
// take care to leave a copy of the alternates file, so that it can be
// manually restored by an administrator if needed.
func removeAlternatesIfOk(ctx context.Context, repo *localrepo.Repo, altFile, backupFile string, logger log.Logger, txManager transaction.Manager) error {
```

**File:** internal/git/objectpool/disconnect.go (L222-246)
```go
	if err := os.Rename(altFile, backupFile); err != nil {
		return err
	}

	rollback := true
	defer func() {
		if !rollback {
			return
		}

		// If we would do a os.Rename, and then someone else comes and clobbers
		// our file, it's gone forever. This trick with os.Link and os.Rename
		// is equivalent to "cp $backupFile $altFile", meaning backupFile is
		// preserved for possible forensic use.
		tmp := backupFile + ".2"

		if err := os.Link(backupFile, tmp); err != nil {
			logger.WithError(err).ErrorContext(ctx, "copy backup alternates file")
			return
		}

		if err := os.Rename(tmp, altFile); err != nil {
			logger.WithError(err).ErrorContext(ctx, "restore backup alternates file")
		}
	}()
```

**File:** internal/gitaly/service/objectpool/alternates.go (L13-18)
```go
// DisconnectGitAlternates is a slightly dangerous RPC. It optimistically hard-links all alternate
// objects we might need, and then temporarily removes (renames) objects/info/alternates and runs
// a connectivity check. If we are unlucky that leaves the repository in a broken state during the
// connectivity check. If we are very unlucky and Gitaly crashes, the repository stays in a broken
// state until an administrator intervenes and restores the backed-up copy of
// objects/info/alternates.
```
