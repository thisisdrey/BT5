[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** internal/gitaly/service/repository/archive.go (L48-59)
```go
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

**File:** internal/gitaly/service/repository/archive.go (L129-181)
```go
func (s *server) validateGetArchivePrecondition(
	ctx context.Context,
	repo gitcmd.RepositoryExecutor,
	commitID string,
	path string,
	exclude []string,
) error {
	objectReader, cancel, err := s.catfileCache.ObjectReader(ctx, repo)
	if err != nil {
		return err
	}
	defer cancel()

	f := catfile.NewTreeEntryFinder(objectReader)
	if path != "." {
		if ok, err := findGetArchivePath(ctx, f, commitID, path); err != nil {
			return err
		} else if !ok {
			return structerr.NewFailedPrecondition("path doesn't exist")
		}
	} else {
		objectInfoReader, cancel, err := s.catfileCache.ObjectInfoReader(ctx, repo)
		if err != nil {
			return err
		}
		defer cancel()

		repoHash, err := repo.ObjectHash(ctx)
		if err != nil {
			return err
		}

		rootTree, err := objectInfoReader.Info(ctx, git.ObjectID(commitID).Revision()+"^{tree}")
		if err != nil {
			return err
		}

		// Root tree is empty, nothing to return.
		if rootTree.ObjectID() == repoHash.EmptyTreeOID {
			return structerr.NewFailedPrecondition("path doesn't exist")
		}
	}

	for i, exclude := range exclude {
		if ok, err := findGetArchivePath(ctx, f, commitID, exclude); err != nil {
			return err
		} else if !ok {
			return structerr.NewFailedPrecondition("exclude[%d] doesn't exist", i)
		}
	}

	return nil
}
```

**File:** internal/gitaly/service/repository/archive.go (L183-193)
```go
func findGetArchivePath(ctx context.Context, f *catfile.TreeEntryFinder, commitID, path string) (ok bool, err error) {
	treeEntry, err := f.FindByRevisionAndPath(ctx, commitID, path)
	if err != nil {
		return false, err
	}

	if treeEntry == nil || len(treeEntry.GetOid()) == 0 {
		return false, nil
	}
	return true, nil
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
