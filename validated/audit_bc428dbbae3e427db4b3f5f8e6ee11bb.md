### Title
Arbitrary symlink creation via unvalidated custom hooks tar extraction leads to path/hook sandbox escape - (File: internal/gitaly/repoutil/custom_hooks.go)

### Summary
`repoutil.ExtractHooks`, invoked by the `SetCustomHooks`/`RestoreCustomHooks` RPCs, extracts an attacker-supplied tar stream into the repository's `custom_hooks` directory by shelling out to the system `tar` binary with no validation of the archive's entry types or link targets [1](#0-0) . Unlike the hardened, purpose-built Go tar extractor used for repository snapshot replication (`extractTarToDirectory`), which explicitly rejects absolute symlinks and validates that both regular files and symlink targets stay within the extraction root [2](#0-1) , the custom-hooks code path performs zero symlink/path sanitization and simply trusts whatever the external `tar` utility does with `TypeSymlink` entries.

### Finding Description
This mirrors the reported WASI bug class: a sandbox is supposed to confine file operations to a directory tree (`custom_hooks/`), but the extraction layer allows the client to plant a symlink inside that "sandbox" whose target points anywhere on the host filesystem, because the extraction implementation never inspects or restricts `Linkname` values (absolute paths, `../` traversal, or paths to executables/scripts outside the repository).

`SetCustomHooks`/`RestoreCustomHooks` stream an arbitrary tar payload straight into `repoutil.SetCustomHooks`, which calls `ExtractHooks` [3](#0-2) . `ExtractHooks` runs:
```
tar -xf - -C <tmpDir> --strip-components 0 custom_hooks
```
with the client-controlled stream piped as stdin, and only checks stderr for the "not found in archive" case [4](#0-3) . There is no equivalent of the checks that `extractTarToDirectory` performs (`filepath.IsAbs(header.Linkname)` rejection, and confirming `resolvedTarget` stays under `targetDir+separator`) [2](#0-1) . Consequently a crafted tar can contain a `custom_hooks/<hookname>` entry of `TypeSymlink` whose `Linkname` is an absolute path (e.g. `/usr/local/bin/some-binary`) or a `../`-escaping relative path, and the system `tar` binary will happily create that symlink on disk with no rejection.

After `SetCustomHooks` completes, the extracted `custom_hooks` directory (now containing an attacker-controlled symlink) is atomically swapped into the repository via `os.Rename` [5](#0-4) . Git subsequently executes files under `custom_hooks/<hookname>` as part of `pre-receive`/`update`/`post-receive` hook processing, following the symlink at execution time — exactly the "symlink now, resolve-and-use later" pattern from the H1 report, escaping the intended `custom_hooks/` confinement.

### Impact Explanation
An actor able to invoke `SetCustomHooksTar`/`RestoreCustomHooks` can place a symlink inside the repository's hook directory that redirects hook execution to an arbitrary path on the Gitaly host filesystem. Depending on what is reachable at that path, this can be leveraged to execute unintended host binaries/scripts under the Gitaly process's privileges during normal push/receive operations, or to corrupt/overwrite unrelated repository state referenced by the symlink — a storage/hook confinement escape distinct from the intended, already-validated `custom_hooks` sandbox that `GetCustomHooks`/`extractTarToDirectory` careful path checks elsewhere in the codebase were designed to prevent.

### Likelihood Explanation
Likelihood is moderate: it requires being able to call `SetCustomHooksTar`/`RestoreCustomHooks` with a crafted tar payload, which is an existing, unauthenticated-at-the-tar-parsing-level RPC surface (Gitaly does not itself inspect the tar contents beyond delegating to the OS `tar` tool). No race condition or additional access is needed beyond crafting the tar stream, unlike more exotic TOCTOU exploits.

### Recommendation
Replace the external `tar` invocation in `ExtractHooks` with a hardened, in-process tar extractor (reusing or sharing the same symlink/absolute-path/traversal validation logic already implemented in `extractTarToDirectory` in `internal/gitaly/service/repository/replicate.go`), rejecting any `TypeSymlink`/`TypeLink` entries whose resolved target escapes the extraction directory, and rejecting absolute link targets outright.

### Proof of Concept
1. Craft a tar archive containing:
   - `custom_hooks/` (directory)
   - `custom_hooks/pre-receive` as a `TypeSymlink` entry with `Linkname = "/usr/local/bin/attacker-script"` (or a relative `../../../../` path to an arbitrary host file).
2. Stream this tar via the `SetCustomHooksTar`/`RestoreCustomHooks` gRPC call for a target repository.
3. `repoutil.ExtractHooks` extracts it with the bare `tar -xf -` command [6](#0-5) , creating the symlink unchecked; `SetCustomHooks` then renames it into place as the live `custom_hooks` directory [5](#0-4) .
4. Trigger a `git push` to the repository; Git executes `custom_hooks/pre-receive`, which resolves through the symlink to the attacker-chosen host path, demonstrating the sandbox escape (contrast with `TestExtractTarToDirectory_SymlinkValidation`, which shows the same class of attack is explicitly blocked for snapshot extraction [7](#0-6) ).

### Citations

**File:** internal/gitaly/repoutil/custom_hooks.go (L55-92)
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

	if err := cmd.Wait(); err != nil {
		stderr := stderrBuilder.String()

		// GNU and BSD tar versions have differing errors when attempting to
		// extract specified members from a valid tar archive. If the tar
		// archive is valid the errors for GNU and BSD tar should have the
		// same prefix, which can be checked to validate whether the expected
		// content is present in the archive for extraction.
		if strings.HasPrefix(stderr, "tar: custom_hooks: Not found in archive") {
			return nil
		}

		return structerr.New("waiting for tar command completion: %w", err).WithMetadata("stderr", stderr)
	}
```

**File:** internal/gitaly/repoutil/custom_hooks.go (L219-223)
```go

	// Move `custom_hooks` from the temporary directory to the repository.
	if err := os.Rename(tempHooksPath, repoHooksPath); err != nil {
		return fmt.Errorf("moving new hooks to repo: %w", err)
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

**File:** internal/gitaly/service/repository/set_custom_hooks.go (L10-42)
```go
// SetCustomHooks sets the git hooks for a repository. The hooks are sent in a
// tar archive containing a `custom_hooks` directory. This directory is
// ultimately extracted to the repository.
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
}
```

**File:** internal/gitaly/service/repository/replicate_test.go (L857-874)
```go
		{
			name: "relative symlink escaping via dotdot is rejected",
			entries: []tarEntry{
				{header: &tar.Header{Name: "valid_before.txt", Typeflag: tar.TypeReg, Mode: 0o644, Size: 3}, body: []byte("aaa")},
				{header: &tar.Header{Name: "escape", Typeflag: tar.TypeSymlink, Linkname: "../../../../tmp"}},
				{header: &tar.Header{Name: "valid_after.txt", Typeflag: tar.TypeReg, Mode: 0o644, Size: 3}, body: []byte("bbb")},
			},
			expectError: "symlink target escapes extraction directory",
		},
		{
			name: "absolute symlink is still rejected",
			entries: []tarEntry{
				{header: &tar.Header{Name: "valid_before.txt", Typeflag: tar.TypeReg, Mode: 0o644, Size: 3}, body: []byte("aaa")},
				{header: &tar.Header{Name: "abs", Typeflag: tar.TypeSymlink, Linkname: "/etc/passwd"}},
				{header: &tar.Header{Name: "valid_after.txt", Typeflag: tar.TypeReg, Mode: 0o644, Size: 3}, body: []byte("bbb")},
			},
			expectError: "absolute symlink not allowed",
		},
```
