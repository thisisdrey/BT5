### Title
Unbounded Disk Write in Bundle-Import RPCs Allows Storage Exhaustion DoS - ([File: internal/git/localrepo/bundle.go])

### Summary
The `CreateRepositoryFromBundle` and `FetchBundle` gRPC handlers accept an arbitrarily long client-streamed bundle payload and copy it directly to disk with no upper bound on the total number of bytes written, mirroring the reported bug class where a caller-supplied quantity is trusted and consumed without validating it against any limit, leaving the system to absorb unbounded/unchecked input.

### Finding Description
Both `CreateRepositoryFromBundle` and `FetchBundle` build a `streamio.Reader` directly from the incoming gRPC stream and hand it to `Repo.FetchBundle`/`CloneBundle`, which in turn calls `createTempBundle`: [1](#0-0) 

`createTempBundle` performs `io.Copy(file, reader)` with no size ceiling, no quota check, and no interruption based on accumulated bytes written: [2](#0-1) 

The RPC handlers themselves also perform no size validation on the incoming stream before or during consumption: [3](#0-2) [4](#0-3) 

Just as the paraspace adapters trusted a caller-declared `value` and forwarded it via `functionCallWithValue` without checking it against `msg.value`, here Gitaly trusts an attacker-controlled stream length (implicitly unbounded, since it is chunked over many gRPC messages) and blindly persists it to the storage backend without checking it against any configured or sane maximum, only bounded by available disk space and gRPC's per-message size limit (which does not bound total stream length across many messages).

### Impact Explanation
An ordinary authenticated user able to invoke `CreateRepositoryFromBundle` (repository import from bundle) or `FetchBundle` (bundle-based fetch/mirror) can stream an effectively unlimited amount of data to the server. Since the data is written to a temporary directory under the storage's data path before any validation of the bundle succeeds, this can exhaust the storage's disk space, degrading or denying service for the Gitaly node and any repositories sharing that storage — a concrete DoS of an RPC handler due to missing resource limits, one of the accepted impact categories.

### Likelihood Explanation
The RPC handlers are reachable directly through an ordinary user-driven action (importing a repository from a bundle, or fetching from a bundle to update a repository) without requiring any privileged Gitaly/Praefect operator access, malicious peer, or token leakage — matching an accepted, unprivileged threat model. The only requirement is being permitted to call the RPC (the same access level as anyone allowed to import a project).

### Recommendation
Enforce an upper bound (backed by configuration, e.g., a maximum bundle/import size) while streaming request chunks in `CreateRepositoryFromBundle`/`FetchBundle`, aborting and cleaning up as soon as the accumulated byte count exceeds the limit, rather than relying solely on `io.Copy` to consume the entire client-controlled stream.

### Proof of Concept
1. Call `CreateRepositoryFromBundle` (or `FetchBundle`) as an ordinary authorized client.
2. After sending the first request containing the target `Repository`, continuously stream `CreateRepositoryFromBundleRequest{Data: ...}` (or `FetchBundleRequest{Data: ...}`) messages with arbitrary filler bytes, never terminating the stream.
3. Observe that `createTempBundle`'s `io.Copy(file, reader)` keeps writing all received bytes to the storage's temporary directory with no size check, consuming disk space until the client stops or the disk fills.

### Citations

**File:** internal/git/localrepo/bundle.go (L195-223)
```go
// createTempBundle copies reader onto the filesystem so that a path can be
// passed to git. git-fetch does not support streaming a bundle over a pipe.
// The caller is responsible for calling the returned cleanup function.
func (repo *Repo) createTempBundle(ctx context.Context, reader io.Reader) (bundlPath string, cleanup func(), returnErr error) {
	tmpDir, cleanup, err := tempdir.New(ctx, repo.GetStorageName(), repo.logger, repo.locator)
	if err != nil {
		return "", nil, fmt.Errorf("create temp bundle: %w", err)
	}

	bundlePath := filepath.Join(tmpDir.Path(), "repo.bundle")

	file, err := os.Create(bundlePath)
	if err != nil {
		cleanup() // Clean up if we fail after creating the temp directory
		return "", nil, fmt.Errorf("create temp bundle: %w", err)
	}
	defer func() {
		if err := file.Close(); err != nil && returnErr == nil {
			returnErr = fmt.Errorf("create temp bundle: %w", err)
		}
	}()

	if _, err = io.Copy(file, reader); err != nil {
		cleanup() // Clean up if we fail after creating the temp directory
		return "", nil, fmt.Errorf("create temp bundle: %w", err)
	}

	return bundlePath, cleanup, nil
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

**File:** internal/gitaly/service/repository/fetch_bundle.go (L10-48)
```go
func (s *server) FetchBundle(stream gitalypb.RepositoryService_FetchBundleServer) error {
	ctx := stream.Context()

	firstRequest, err := stream.Recv()
	if err != nil {
		return structerr.NewInternal("first request: %w", err)
	}

	if err := s.locator.ValidateRepository(ctx, firstRequest.GetRepository()); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	firstRead := true
	reader := streamio.NewReader(func() ([]byte, error) {
		if firstRead {
			firstRead = false
			return firstRequest.GetData(), nil
		}

		request, err := stream.Recv()
		return request.GetData(), err
	})

	repo := s.localRepoFactory.Build(firstRequest.GetRepository())

	// Verify that the repository actually exists.
	if _, err := repo.Path(ctx); err != nil {
		return err
	}

	opts := &localrepo.FetchBundleOpts{
		UpdateHead: firstRequest.GetUpdateHead(),
	}

	if err := repo.FetchBundle(ctx, s.txManager, reader, opts); err != nil {
		return structerr.NewInternal("%w", err)
	}

	return stream.SendAndClose(&gitalypb.FetchBundleResponse{})
```
