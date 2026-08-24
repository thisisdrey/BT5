### Title
Tar-slip (symlink-based path traversal) in `custom_hooks` extraction allows writing outside the repository storage - ([File: internal/gitaly/repoutil/custom_hooks.go])

### Finding Description
`repoutil.ExtractHooks` (internal/gitaly/repoutil/custom_hooks.go:55-95) blindly pipes attacker-supplied bytes into the system `tar` binary via `command.New(ctx, logger, []string{"tar", "-xf", "-", "-C", path, "--strip-components", stripComponents, CustomHooksDir}, ...)` [1](#0-0) . No entry is inspected or filtered by Gitaly before extraction: there is no check that a `custom_hooks` member is a regular file/dir (not a symlink), and no post-extraction confinement check (e.g. `os.Lstat` walk verifying every path under `tmpDir.Path()`).

Both `SetCustomHooksServer` and `RestoreCustomHooksServer` stream the raw client-provided tar bytes straight into `repoutil.SetCustomHooks` → `ExtractHooks`, only validating the `Repository` message via `s.locator.ValidateRepository`, not the tar payload itself [2](#0-1) . The existing test suite even documents that `GetCustomHooks` will faithfully re-serialize a `custom_hooks` entry that is itself a symlink pointing anywhere on the filesystem (`TestGetCustomHooks_symlink`, target `/var/empty`) [3](#0-2) , confirming that a symlinked `custom_hooks` entry is a legitimate, round-trippable tar shape in this pipeline (e.g. via project export/import replay of a `GetCustomHooksResponse`).

Because a single tar stream can contain an entry `custom_hooks` typed as a symlink to an attacker-chosen target followed by further entries such as `custom_hooks/evil`, GNU tar's extraction of that second entry resolves the `custom_hooks` path component through the just-created symlink and writes the payload at the symlink target rather than inside `tmpDir.Path()`. This is the classic "tar-slip" archive-extraction escape (CWE-22): the `-C` confinement flag only fixes tar's *own* cwd, it does not prevent path traversal through symlinks created earlier in the same extraction pass. No `storage.ValidateRelativePath`-style logic, symlink rejection, or extraction sandboxing exists anywhere in this code path to stop it. Standard preconditions (attacker owning/importing a repo whose custom_hooks tarball GitLab Rails later replays into a target repo they control) are consistent with an unprivileged, no-special-role GitLab user.

### Impact Explanation
If GNU tar on the server follows the intra-archive symlink (the common default behavior for symlink entries created within the same extraction, distinct from tar's separate leading-`/`/`..`-name stripping logic), an attacker can write an arbitrary file to an arbitrary path reachable by the Gitaly process user — e.g. planting a malicious executable, cron entry, or SSH-authorized-key — outside the repository's `custom_hooks` directory and outside storage confinement. Combined with GitLab's mandatory custom-hooks dispatch (pre-receive/update/post-receive), this can also let an attacker's hook payload run in unexpected disk locations before push access checks are meaningfully isolated, matching the "storage-path escape" / "hook or quarantine bypass" impact classes in scope.

### Likelihood Explanation
The only precondition is that the attacker controls a repository (owns/imports it) and can trigger a `custom_hooks` tar (export/fork/import) that GitLab Rails later replays into a target repo via `SetCustomHooksRequest`/`RestoreCustomHooksRequest` — both reachable by an ordinary authenticated user via existing GitLab import/fork workflows. No admin role, no secret, and no non-default configuration are required; the vulnerable code performs zero content inspection of the tar stream.

### Recommendation
- Reject or ignore any tar entry under `custom_hooks/` whose `Typeflag` is `TypeSymlink` or `TypeLink` before invoking `tar -x` (parse with Go's `archive/tar` and re-serialize only regular files/dirs, or add `--exclude` filtering).
- After extraction, verify (via `filepath.EvalSymlinks`/`os.Lstat` walk) that every path physically resolves under `tmpDir.Path()` before proceeding with the rename into `repoHooksPath`.
- Alternatively extract with a hardened extractor (e.g. Go's `archive/tar` reader driving manual, path-validated writes) instead of shelling out to the system `tar` binary, so that entry names and symlink targets can be validated against `storage.ValidateRelativePath` semantics prior to any filesystem write.

### Proof of Concept
```go
func TestExtractHooks_symlinkEscape(t *testing.T) {
	tmpDir := t.TempDir()
	outsideDir := t.TempDir() // simulate a location outside the sandbox

	var buf bytes.Buffer
	w := tar.NewWriter(&buf)
	// 1. Make "custom_hooks" itself a symlink pointing outside tmpDir.
	require.NoError(t, w.WriteHeader(&tar.Header{
		Name:     "custom_hooks",
		Typeflag: tar.TypeSymlink,
		Linkname: outsideDir,
	}))
	// 2. A subsequent entry that is resolved *through* that symlink.
	content := []byte("#!/bin/sh\necho pwned\n")
	require.NoError(t, w.WriteHeader(&tar.Header{
		Name: "custom_hooks/evil",
		Mode: 0o755,
		Size: int64(len(content)),
	}))
	_, err := w.Write(content)
	require.NoError(t, err)
	require.NoError(t, w.Close())

	err = ExtractHooks(context.Background(), testhelper.NewLogger(t), &buf, tmpDir, false)
	require.NoError(t, err)

	// Assert no file escaped tmpDir.
	_, statErr := os.Lstat(filepath.Join(outsideDir, "evil"))
	require.True(t, os.IsNotExist(statErr), "expected no file written outside tmpDir, escape occurred")
}
```
Expected (vulnerable) result: `outsideDir/evil` exists with the attacker payload, proving the write escaped `tmpDir.Path()`.

### Citations

**File:** internal/gitaly/repoutil/custom_hooks.go (L69-77)
```go
	cmdArgs := []string{"-xf", "-", "-C", path, "--strip-components", stripComponents, CustomHooksDir}

	var stderrBuilder strings.Builder
	cmd, err := command.New(ctx, logger, append([]string{"tar"}, cmdArgs...),
		command.WithStdin(buf),
		command.WithStderr(&stderrBuilder))
	if err != nil {
		return fmt.Errorf("executing tar command: %w", err)
	}
```

**File:** internal/gitaly/service/repository/set_custom_hooks.go (L13-41)
```go
func (s *server) SetCustomHooks(stream gitalypb.RepositoryService_SetCustomHooksServer) error {
	ctx := stream.Context()

	firstRequest, err := stream.Recv()
	if err != nil {
		return structerr.NewInternal("getting first request: %w", err)
	}

	repo := firstRequest.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repo); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	reader := streamio.NewReader(func() ([]byte, error) {
		if firstRequest != nil {
			data := firstRequest.GetData()
			firstRequest = nil
			return data, nil
		}

		request, err := stream.Recv()
		return request.GetData(), err
	})

	if err := repoutil.SetCustomHooks(ctx, s.logger, s.locator, s.txManager, reader, repo); err != nil {
		return structerr.NewInternal("setting custom hooks: %w", err)
	}

	return stream.SendAndClose(&gitalypb.SetCustomHooksResponse{})
```

**File:** internal/gitaly/repoutil/custom_hooks_test.go (L68-93)
```go
func TestGetCustomHooks_symlink(t *testing.T) {
	t.Parallel()

	ctx := testhelper.Context(t)
	cfg := testcfg.Build(t)
	_, repoPath := gittest.CreateRepository(t, ctx, cfg, gittest.CreateRepositoryConfig{
		SkipCreationViaService: true,
	})

	linkTarget := "/var/empty"
	require.NoError(t, os.Symlink(linkTarget, filepath.Join(repoPath, "custom_hooks")), "Could not create custom_hooks symlink")

	var hooks bytes.Buffer
	require.NoError(t, GetCustomHooks(ctx, testhelper.NewLogger(t), repoPath, &hooks))

	reader := tar.NewReader(&hooks)
	file, err := reader.Next()
	require.NoError(t, err)

	require.Equal(t, "custom_hooks", file.Name, "tar entry name")
	require.Equal(t, byte(tar.TypeSymlink), file.Typeflag, "tar entry type")
	require.Equal(t, linkTarget, file.Linkname, "link target")

	_, err = reader.Next()
	require.Equal(t, io.EOF, err, "custom_hooks should have been the only entry")
}
```
