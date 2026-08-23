## Finding [1](#0-0) 

### Title
Unbounded in-memory read of pre-receive hook stdin allows attacker-driven memory exhaustion - (File: internal/gitaly/hook/prereceive.go)

### Summary
`GitLabHookManager.PreReceiveHook` reads the entire git-receive-pack reference-update list from an ordinary push into memory with `io.ReadAll(stdin)` and no size cap, unlike other Gitaly read paths (e.g. `Repo.ReadObjectWithLimit`) that explicitly bound reads with `io.LimitReader`.

### Finding Description
When a client performs `git push`, git-receive-pack invokes the pre-receive hook via `gitaly-hooks`, which streams the reference-update command list to Gitaly's `HookService.PreReceiveHook` RPC as `PreReceiveHookRequest.Stdin` chunks [2](#0-1) [3](#0-2) . The server-side handler reassembles the stream into an `io.Reader` and hands it to the hook manager [4](#0-3) .

`GitLabHookManager.PreReceiveHook` then does:
```go
changes, err := io.ReadAll(stdin)
```
with no length limit, before ever validating the request or repository state [1](#0-0) . The number of reference updates in a single `git push` (and thus the size of this stdin stream) is controlled entirely by the pushing client — one line per ref update, each up to a full pktline (`MaxPktSize = 65520` bytes) [5](#0-4)  — and there is no cap in this code path on the total number of ref-update lines or aggregate stdin size before the whole payload is buffered into a single `[]byte` in Gitaly's memory.

This contrasts with other resource-conscious code in the same codebase, e.g. `Repo.ReadObjectWithLimit`, which deliberately wraps reads in `io.LimitReader` to bound memory usage [6](#0-5) , and Gitaly's documented backpressure/concurrency-limiting mechanisms, which are aimed at RPC concurrency rather than per-request payload size [7](#0-6) .

### Impact Explanation
An authenticated user permitted to push to a repository can construct a single push touching an extremely large number of references (or a push that otherwise generates a very large reference-update/push-cert body). Because the entire body is buffered unconditionally via `io.ReadAll` before any size check, this can drive excessive memory allocation per pre-receive hook invocation on the Gitaly node, and since PreReceiveHook executes for every push, concurrent or repeated large pushes can amplify memory pressure, degrading or crashing the Gitaly process (denial of service) — directly analogous to the jose2go uncontrolled-resource-consumption class.

### Likelihood Explanation
Low-to-medium: it requires an authenticated actor with push access to send a push whose reference-update list is unusually large. No malicious peer, MITM, or leaked-token conditions are needed — a normal authorized push client suffices, matching the "ordinary user push" reachability criterion.

### Recommendation
Bound the stdin read in `GitLabHookManager.PreReceiveHook` (and check `PostReceiveHook`/`UpdateHook` for the same pattern) with an `io.LimitReader` sized to a configurable maximum, rejecting or truncating with a clear error when exceeded, consistent with the limiting pattern already used in `ReadObjectWithLimit`.

### Proof of Concept
1. As an authenticated user with push access to a repository, craft a `git push` with an artificially large number of ref updates (e.g., scripted push of many branch/tag refs, or a maximal push-cert payload) so the receive-pack reference-update list sent to the pre-receive hook is very large.
2. Observe that `internal/gitaly/hook/prereceive.go`'s `io.ReadAll(stdin)` buffers the entire payload into memory before any validation of the repository or user occurs, with no configured upper bound, causing elevated memory usage proportional to the size the client chooses to send.

### Citations

**File:** internal/gitaly/hook/prereceive.go (L69-78)
```go
func (m *GitLabHookManager) PreReceiveHook(ctx context.Context, repo *gitalypb.Repository, pushOptions, env []string, stdin io.Reader, stdout, stderr io.Writer) error {
	payload, err := gitcmd.HooksPayloadFromEnv(env)
	if err != nil {
		return structerr.NewInternal("extracting hooks payload: %w", err)
	}

	changes, err := io.ReadAll(stdin)
	if err != nil {
		return structerr.NewInternal("reading stdin from request: %w", err)
	}
```

**File:** proto/hook.proto (L77-87)
```text
// PreReceiveHookRequest ...
message PreReceiveHookRequest {
  // repository ...
  Repository repository = 1 [(target_repository)=true];
  // environment_variables ...
  repeated string environment_variables = 2;
  // stdin ...
  bytes stdin = 4;
  // git_push_options ...
  repeated string git_push_options = 5;
}
```

**File:** cmd/gitaly-hooks/hooks.go (L301-317)
```go
func preReceiveHook(ctx context.Context, payload gitcmd.HooksPayload, hookClient gitalypb.HookServiceClient, args []string) error {
	preReceiveHookStream, err := hookClient.PreReceiveHook(ctx)
	if err != nil {
		return fmt.Errorf("error when getting preReceiveHookStream client for: %w", err)
	}

	if err := preReceiveHookStream.Send(&gitalypb.PreReceiveHookRequest{
		Repository:           payload.Repo,
		EnvironmentVariables: os.Environ(),
		GitPushOptions:       gitPushOptions(),
	}); err != nil {
		return fmt.Errorf("error when sending request for pre-receive hook: %w", err)
	}

	f := sendFunc(streamio.NewWriter(func(p []byte) error {
		return preReceiveHookStream.Send(&gitalypb.PreReceiveHookRequest{Stdin: p})
	}), preReceiveHookStream, os.Stdin)
```

**File:** internal/gitaly/service/hook/pre_receive.go (L17-49)
```go
func (s *server) PreReceiveHook(stream gitalypb.HookService_PreReceiveHookServer) error {
	firstRequest, err := stream.Recv()
	if err != nil {
		return structerr.NewInternal("receiving first request: %w", err)
	}

	if err := validatePreReceiveHookRequest(stream.Context(), s.locator, firstRequest); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}
	repository := firstRequest.GetRepository()

	stdin := streamio.NewReader(func() ([]byte, error) {
		req, err := stream.Recv()
		return req.GetStdin(), err
	})

	var m sync.Mutex
	stdout := streamio.NewSyncWriter(&m, func(p []byte) error {
		return stream.Send(&gitalypb.PreReceiveHookResponse{Stdout: p})
	})
	stderr := streamio.NewSyncWriter(&m, func(p []byte) error {
		return stream.Send(&gitalypb.PreReceiveHookResponse{Stderr: p})
	})

	if err := s.manager.PreReceiveHook(
		stream.Context(),
		repository,
		firstRequest.GetGitPushOptions(),
		firstRequest.GetEnvironmentVariables(),
		stdin,
		stdout,
		stderr,
	); err != nil {
```

**File:** internal/git/pktline/pktline.go (L20-23)
```go
	// MaxPktSize is the maximum size of content of a Git pktline side-band-64k
	// packet, including size of length and band number
	// https://gitlab.com/gitlab-org/git/-/blob/v2.30.0/pkt-line.h#L216
	MaxPktSize = 65520
```

**File:** internal/git/localrepo/objects.go (L108-116)
```go
	// io.LimitReader returns EOF once the limit is reached. This leaves the
	// object in a potentially half-read state as we might've read less than
	// the total object size. This will be problematic if the process is reused
	// for a future cat-file invocation, but thankfully the cache can detect if
	// the object is dirty, and discard it from the cache.
	data, err := io.ReadAll(io.LimitReader(object, limit))
	if err != nil {
		return nil, fmt.Errorf("read object from reader: %w", err)
	}
```

**File:** doc/backpressure.md (L1-24)
```markdown
# Request limiting in Gitaly

In the GitLab ecosystem, Gitaly is the service that is at the bottom of the
stack for Git data access. This means that when there is a surge of
requests to retrieve or change a piece of Git data, the I/O happens in Gitaly.
This can lead to Gitaly being overwhelmed due to system resource exhaustion
because all Git access goes through Gitaly.

If there is a surge of traffic beyond what Gitaly can handle, Gitaly should
be able to push back on the client calling. Gitaly shouldn't subserviently agree
to process more than it can handle.

We employ concurrency limiting as our primary backpressure mechanism in Gitaly.

## Concurrency queue

Limit the number of concurrent RPCs that are in flight on each Gitaly node for each
repository per RPC using `[[concurrency]]` configuration:

```toml
[[concurrency]]
rpc = "/gitaly.SmartHTTPService/PostUploadPackWithSidechannel"
max_per_repo = 1
```
```
