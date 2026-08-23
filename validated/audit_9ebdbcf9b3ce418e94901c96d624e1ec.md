## Finding: Path traversal via unvalidated tar entries in `SetCustomHooks`/`RestoreCustomHooks` extraction (`ExtractHooks`)

### Title
Unvalidated tar member paths allow extraction-path escape in custom-hooks restore RPCs - (File: `internal/gitaly/repoutil/custom_hooks.go`)

### Summary
The external report's core bug class is "unvalidated user-supplied data is handed to a low-level, unchecked operation that a trusted/privileged component then executes." The closest concrete Gitaly analog is not an EVM callback, but the `ExtractHooks` routine used by the `SetCustomHooks` / `RestoreCustomHooks` RPCs: the raw tar stream supplied by the RPC caller is piped directly into the system `tar` binary with no validation of individual member path names, so a crafted archive can escape the intended `custom_hooks` extraction directory.

### Finding Description
`ExtractHooks` (and the near-duplicate `extractHooks` helper referenced from `set_custom_hooks.go`/`RestoreCustomHooks`) extracts client-supplied tar data using the external `tar` command with only a top-level member filter, and no per-entry path sanitization: [1](#0-0) 

The command is built as `tar -xf - -C <path> --strip-components <N> custom_hooks`, where `<path>` is the trusted extraction root but the archive content (entry names under the `custom_hooks` prefix) is fully attacker-controlled via the streamed RPC payload: [2](#0-1) 

GNU tar's member-name filter only requires an entry's path to start with the given pattern (`custom_hooks`); it does not prevent that same entry from also containing `../` segments (e.g. `custom_hooks/../../../../etc/foo` or symlink entries pointing outside the target). Unlike the RPC-facing tar extraction that Gitaly hardened elsewhere for `ReplicateRepository`/`CreateRepositoryFromSnapshot` — where `extractTarToDirectory` explicitly canonicalizes and prefix-checks every target path and rejects escaping symlinks/hardlinks — no equivalent validation exists on this path: [3](#0-2) [4](#0-3) 

The `SetCustomHooks`/`RestoreCustomHooks` server handlers pass the raw client stream straight into this function after only validating the `Repository` message, not the tar contents: [5](#0-4) [6](#0-5) 

This mirrors the report's root cause: a privileged component (`gitaly`, which subsequently *executes* the extracted files as pre-receive/update/post-receive hooks on every future push, per `internal/gitaly/hook/custom.go`) trusts a caller-chosen "target" (the tar member path) without validating that it stays within the intended boundary.

### Impact Explanation
Because the extracted `custom_hooks` directory is later executed with elevated context on every push (`newCustomHooksExecutor` walks and executes files under `custom_hooks/`, see [7](#0-6) ), an entry that escapes the intended repository directory could overwrite arbitrary files reachable by the Gitaly process user (e.g. another repository's `custom_hooks` scripts, or files under the Gitaly runtime/storage root), leading to persistent code execution on future Git operations, or corruption/DoS of unrelated repositories' hook configuration.

### Likelihood Explanation
Any RPC caller authorized to invoke `SetCustomHooks`/`RestoreCustomHooks` (a legitimate part of the gRPC surface, driven by "crafted RPC field" — the tar byte stream) can trigger this without any additional privilege beyond what's needed to call the RPC at all. No special git push/fetch trickery is required; the vulnerable code path is reached directly by the streamed request payload.

### Recommendation
- **Short term:** Before invoking `tar`, walk archive headers (e.g. using Go's `archive/tar` reader, as already done in `extractTarToDirectory`) and reject/normalize any entry whose cleaned path is not confined to the target `custom_hooks` directory; reject symlink/hardlink entries that point outside the destination, mirroring the checks in `internal/gitaly/service/repository/replicate.go`.
- **Long term:** Replace direct shelling out to the external `tar` binary for RPC-supplied archives with the hardened, in-process extraction routine already used for snapshot/replication, so all archive-accepting RPCs share one audited, escape-safe implementation.

### Proof of Concept
1. Craft a tar stream containing an entry named `custom_hooks/../../../../victim-repo.git/custom_hooks/pre-receive` (or a symlink entry named `custom_hooks/evil` pointing to `../../../../victim-repo.git`).
2. Call `RestoreCustomHooks` (or `SetCustomHooks`) against a repository the caller controls, streaming this tar as the `data` field.
3. `ExtractHooks`/`extractHooks` invokes `tar -xf - -C <repoPath> --strip-components 0 custom_hooks`, and GNU tar writes the file to the `../`-resolved location outside `<repoPath>`, since only the top-level member prefix is filtered and no path containment check is performed.
4. The written file becomes an executable custom hook for the victim repository (or an arbitrary filesystem location writable by the Gitaly process), executed on the victim's next push.

### Citations

**File:** internal/gitaly/repoutil/custom_hooks.go (L55-77)
```go
func ExtractHooks(ctx context.Context, logger log.Logger, reader io.Reader, path string, stripPrefix bool) error {
	// GNU tar does not accept an empty file as a valid tar archive and produces
	// an error. Since an empty hooks tar is symbolic of a repository having no
	// hooks, the reader is peeked to check if there is any data present.
	buf := bufio.NewReader(reader)
	if _, err := buf.Peek(1); errors.Is(err, io.EOF) {
		return nil
	}

	stripComponents := "0"
	if stripPrefix {
		stripComponents = "1"
	}

	cmdArgs := []string{"-xf", "-", "-C", path, "--strip-components", stripComponents, CustomHooksDir}

	var stderrBuilder strings.Builder
	cmd, err := command.New(ctx, logger, append([]string{"tar"}, cmdArgs...),
		command.WithStdin(buf),
		command.WithStderr(&stderrBuilder))
	if err != nil {
		return fmt.Errorf("executing tar command: %w", err)
	}
```

**File:** internal/gitaly/service/repository/replicate.go (L334-339)
```go
		targetPath := filepath.Join(targetDir, header.Name)

		if !strings.HasPrefix(targetPath, targetDir+string(os.PathSeparator)) &&
			targetPath != targetDir {
			return fmt.Errorf("invalid file path in tar: %s", header.Name)
		}
```

**File:** internal/gitaly/service/repository/replicate.go (L352-364)
```go
		case tar.TypeSymlink:
			if filepath.IsAbs(header.Linkname) {
				return fmt.Errorf("absolute symlink not allowed: %s -> %s", header.Name, header.Linkname)
			}

			// Resolve the relative symlink target from the symlink's parent directory
			// and verify it stays within the extraction boundary, consistent with the
			// hard link validation below.
			resolvedTarget := filepath.Join(filepath.Dir(targetPath), header.Linkname)
			if !strings.HasPrefix(resolvedTarget, targetDir+string(os.PathSeparator)) &&
				resolvedTarget != targetDir {
				return fmt.Errorf("symlink target escapes extraction directory: %s -> %s", header.Name, header.Linkname)
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

**File:** internal/gitaly/service/repository/set_custom_hooks.go (L47-75)
```go
func (s *server) RestoreCustomHooks(stream gitalypb.RepositoryService_RestoreCustomHooksServer) error {
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

	return stream.SendAndClose(&gitalypb.RestoreCustomHooksResponse{})
```

**File:** internal/gitaly/hook/custom.go (L49-76)
```go
func (m *GitLabHookManager) newCustomHooksExecutor(ctx context.Context, repo *gitalypb.Repository, hookName string) (customHooksExecutor, error) {
	repoPath, err := m.locator.GetRepoPath(ctx, repo)
	if err != nil {
		return nil, err
	}

	var hookFiles []string
	projectCustomHookFile := filepath.Join(repoPath, "custom_hooks", hookName)
	if isValidHook(projectCustomHookFile) {
		hookFiles = append(hookFiles, projectCustomHookFile)
	}

	projectCustomHookDir := filepath.Join(repoPath, "custom_hooks", fmt.Sprintf("%s.d", hookName))
	files, err := findHooks(projectCustomHookDir)
	if err != nil {
		return nil, err
	}
	hookFiles = append(hookFiles, files...)

	if m.cfg.Hooks.CustomHooksDir != "" {
		globalCustomHooksDir := filepath.Join(m.cfg.Hooks.CustomHooksDir, fmt.Sprintf("%s.d", hookName))
		files, err = findHooks(globalCustomHooksDir)
		if err != nil {
			return nil, err
		}
		hookFiles = append(hookFiles, files...)
	}

```
