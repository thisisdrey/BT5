### Title
Unsanitized tar extraction from attacker-influenced HTTP snapshot allows path traversal write outside repo - (File: internal/gitaly/service/repository/create_repository_from_snapshot.go)

### Finding Description
`CreateRepositoryFromSnapshot` calls `repoutil.Create`, which invokes `s.untar(ctx, path, in)` where `path` is the freshly created repo directory from `s.locator.GetRepoPath`. `s.untar` fetches `in.GetHttpUrl()` (optionally pinned to `in.GetResolvedAddress()` to avoid DNS-rebinding) and pipes the raw HTTP response body directly into `tar -C path -xvf -` via `command.New(..., command.WithStdin(rsp.Body))` with no inspection of the tar entries. [1](#0-0) 
There is no validation of tar entry names (e.g., rejecting `../` segments or absolute paths) before or during extraction, and the code comment itself states: "NOTE: The received archive is trusted *a lot*. Before pointing this RPC at endpoints not under our control, it should undergo a lot of hardening." [2](#0-1) 
GNU `tar -x` by default honors `../` relative traversal and absolute paths present in archive members, writing outside `-C path` unless `--no-absolute-names`/path-sanitization is explicitly used. Since this code never filters entries, a crafted tar stream can escape the target repository directory, e.g., planting a `custom_hooks/pre-receive` script into a sibling repository or an arbitrary `config` file elsewhere in the storage root.

The `HttpUrl`/`HttpAuth`/`ResolvedAddress` fields are RPC request fields controlled by whatever caller issues the `CreateRepositoryFromSnapshotRequest`. In stock GitLab, this RPC is invoked by GitLab Rails (or Gitaly-to-Gitaly for repo backup/restore/geo-style flows) with a URL Rails constructs, not something a GitLab end-user directly supplies over the standard fork/import/push UI. I could not find, in the indexed portion of this repo, evidence of a code path where a plain unprivileged GitLab user (without admin/operator capability) can set `HttpUrl` to an attacker-chosen endpoint through ordinary fork or import operations — that binding happens in GitLab Rails, which is out of scope per the rules ("reject anything ... [depending on] a bug in GitLab Rails ... rather than a bug in Gitaly"). Without concrete evidence that an unprivileged, unmodified GitLab flow lets a normal user control `HttpUrl` for this RPC, the "unprivileged attacker" precondition required by the rules is unverified from the Gitaly codebase alone.

### Impact Explanation
If the precondition holds (attacker-influenced `HttpUrl`), this would be a genuine archive-extraction path-confinement violation (PATH_CONFINEMENT), allowing cross-repository file writes, hook planting, and potentially code execution via `custom_hooks`/`hooks`, matching a high-severity GitLab bounty impact class (Repository/Server-side file write leading to hook execution).

### Likelihood Explanation
The Gitaly-side code has zero mitigation against malicious tar entries — likelihood *given attacker control of `HttpUrl`* is high and deterministic (any tar client accepting `../` or absolute paths, e.g., GNU tar, would extract them). However, the rules explicitly require that the attacker capability be reachable via a genuinely unprivileged GitLab surface (fork/push/import) without relying on a GitLab Rails bug, and I could not confirm within the Gitaly repo that such a direct, unprivileged, attacker-controlled binding of `HttpUrl` exists for this RPC — this RPC's callers and URL construction live in GitLab Rails, outside this repo's indexed content.

### Recommendation
Regardless of the reachability question, Gitaly should not trust arbitrary tar streams: extract using a safe extraction routine (e.g., Go's `archive/tar` with entry-name validation rejecting `..`, absolute paths, and symlink/hardlink escapes) or invoke system `tar` with `--no-absolute-names` and post-validate that all resulting paths stay under `path` via `filepath.Clean`/`storage.ValidateRelativePath`-style checks before allowing extraction to complete.

### Proof of Concept
```go
// Pseudo Go test sketch (not verified against actual RPC wiring in this task):
// 1. Start httptest.Server serving a tar body with an entry named
//    "../../victim/custom_hooks/pre-receive" containing shell content.
// 2. Issue CreateRepositoryFromSnapshotRequest{Repository: repoA, HttpUrl: server.URL}.
// 3. Assert that victim/custom_hooks/pre-receive exists outside repoA's GetRepoPath.
```
Note: this PoC targets the Gitaly RPC directly (bypassing GitLab Rails), which satisfies the technical exploitability of `s.untar` but not necessarily the "unprivileged GitLab user" precondition, which could not be confirmed from this repository alone.

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

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L129-145)
```go
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

```
