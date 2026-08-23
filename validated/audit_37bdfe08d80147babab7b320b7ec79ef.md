`RegisterAll` in `internal/gitaly/service/setup/register.go` registers `InternalGitaly` (including the state-mutating `StorePoolMetadata` RPC) on the exact same `*grpc.Server` as every ordinary, externally-reachable service (`RepositoryService`, `SmartHTTPService`, `SSHService`, etc.), via a single `RegisterAll(srv, deps)` call. [1](#0-0) 

## Title
`InternalGitaly.StorePoolMetadata` is registered on the externally-reachable gRPC server and is callable by any authenticated Gitaly client, not just Praefect/other Gitaly nodes - (File: `internal/gitaly/service/setup/register.go`, `internal/gitaly/service/internalgitaly/store_pool_metadata.go`)

### Summary
`InternalGitaly` is documented and typed as "meant to be served by a Gitaly node, but only reachable by Praefect or other Gitalies" [2](#0-1)  and its generated client/server interfaces repeat this restriction [3](#0-2) . Despite this, `RegisterAll`, the function used to register services on Gitaly's normal (external) gRPC server, calls `gitalypb.RegisterInternalGitalyServer(srv, internalgitaly.NewServer(deps))` in the same block as `RepositoryService`, `SmartHTTPService`, `SSHService`, etc. [4](#0-3) . There is no separate registrar that keeps `InternalGitaly` off the external listener; `GitalyServerFactory` only distinguishes "external" vs "internal" *sockets*, not which services are registered on each [5](#0-4) , and the actual choice of which registrar function to call on which listener is a deployment/wiring decision in `cli/gitaly/serve.go`, not enforced by the RPC handler itself.

This is the same bug class as the reported `receiveCollateral()` finding: a handler intended to be called only by a privileged internal component (`ActivePool` / Praefect) performs no caller-identity check of its own and instead relies entirely on network/deployment topology (or, in the Yeti case, the honor system of the caller) to restrict access.

### Finding Description
Unlike `hook.SignalPostReceiveReady`, which explicitly checks for a Praefect backchannel connection at the top of the handler and rejects calls without one with `PermissionDenied: "can only be called via Praefect backchannel"` [6](#0-5) , none of the `InternalGitaly` RPC implementations (`WalkRepos`, `ScanPoolMetadata`, `StorePoolMetadata`, `ListPoolMetadata`, `ListPoolUpstreams`) perform any analogous caller check. `ScanPoolMetadata`'s implementation, for example, goes straight from extracting `storage_name` off the request to walking the filesystem and streaming back pool-relationship data, with only a "storage exists" check `s.locator.GetStorageByName(ctx, storageName)` [7](#0-6) . `StorePoolMetadata` is a `MUTATOR` per the proto annotation [8](#0-7)  that writes repository-to-pool relationship records into the local pool metadata database, again with no caller-identity gate visible in the handler.

Whether this is exploitable in a given deployment depends entirely on whether `RegisterAll` (which includes `InternalGitaly`) is wired only to the loopback/internal socket, or also to the externally reachable listener that ordinary authenticated Gitaly gRPC clients (any client holding the shared gitaly auth token, e.g., gitlab-shell/gitlab-workhorse credentials, not a Praefect-only secret) can reach. Because the same shared V2 HMAC token is used for both the "external" and "internal" listeners (`CreateExternal`/`CreateInternal` both apply the identical auth interceptor, differing only in security transport options) [5](#0-4) , any principal that can authenticate to Gitaly at all — not specifically Praefect — can invoke `StorePoolMetadata`/`ScanPoolMetadata`/`WalkRepos`/`ListPoolMetadata`/`ListPoolUpstreams` if `RegisterAll` is reachable from that same auth domain.

### Impact Explanation
If reachable, `WalkRepos` and `ScanPoolMetadata` disclose the full repository listing and object-pool/alternates linkage of a storage to a caller who should not have storage-wide enumeration rights (cross-repository information disclosure), and `StorePoolMetadata` allows an unprivileged-but-authenticated caller to inject or corrupt pool-to-repository metadata records that other Gitaly logic (via `relational.PoolStore`) may later trust for object-pool/alternates decisions — a potential integrity/DoS vector for housekeeping and pool-membership bookkeeping.

### Likelihood Explanation
The likelihood hinges on deployment wiring that I could not fully confirm from the indexed files (`cli/gitaly/serve.go` was only found via grep and not fully read in this session — I was not able to trace whether `RegisterAll` is invoked exclusively against `CreateInternal()` or also against `CreateExternal()` in the production `serve` command). The documentation/comment intent is clear ("only reachable by Praefect or other Gitalies"), but the code enforces this only through socket separation and shared authentication, not through an explicit in-handler caller check (unlike `SignalPostReceiveReady`, which does check). This makes the analog concrete only if `RegisterAll` output ends up bound to a listener reachable by non-Praefect, non-Gitaly-node holders of the shared auth token.

### Recommendation
Add an explicit caller check to every `InternalGitaly` RPC handler (mirroring the pattern already used in `hook.SignalPostReceiveReady`), verifying the request arrives via the Praefect backchannel or from a recognized peer Gitaly node, rather than relying solely on which socket the service happens to be registered on. Additionally, confirm in `cli/gitaly/serve.go` that `RegisterAll`'s `InternalGitaly` registration is only ever bound to the internal-only listener created via `CreateInternal`, and consider splitting `InternalGitaly` out of `RegisterAll` into a dedicated internal-only registrar so future refactors cannot accidentally expose it externally.

### Proof of Concept
Not independently verified against a running deployment in this session (no filesystem/terminal access). Conceptually: any client holding a valid Gitaly V2 bearer token for the storage's Gitaly node dials the socket/address that `RegisterAll` was bound to and calls `InternalGitaly.StorePoolMetadata` directly (as the test/CLI code in `internal/cli/gitaly/subcmd_pool.go` and `internal/cli/common/pool.go` already demonstrates is a normal, unauthenticated-beyond-the-shared-token client flow) [9](#0-8) , without needing to be Praefect or another Gitaly node.

### Citations

**File:** internal/gitaly/service/setup/register.go (L117-151)
```go
// RegisterAll will register all the known gRPC services on  the provided gRPC service instance.
func RegisterAll(srv *grpc.Server, deps *service.Dependencies) {
	gitalypb.RegisterAnalysisServiceServer(srv, analysis.NewServer(deps))
	gitalypb.RegisterBackupServiceServer(srv, gitalybackup.NewServer(deps))
	gitalypb.RegisterBlobServiceServer(srv, blob.NewServer(deps))
	gitalypb.RegisterCleanupServiceServer(srv, cleanup.NewServer(deps))
	gitalypb.RegisterCommitServiceServer(srv, commit.NewServer(deps))
	gitalypb.RegisterDiffServiceServer(srv, diff.NewServer(deps))
	gitalypb.RegisterOperationServiceServer(srv, operations.NewServer(deps))
	gitalypb.RegisterRefServiceServer(srv, ref.NewServer(deps))
	gitalypb.RegisterRepositoryServiceServer(srv, repository.NewServer(deps))
	gitalypb.RegisterSSHServiceServer(srv, ssh.NewServer(deps,
		ssh.WithPackfileNegotiationMetrics(sshPackfileNegotiationMetrics),
		ssh.WithPackfileNegotiationDeepenMetrics(sshPackfileNegotiationDeepenMetrics),
		ssh.WithUploadPackServedBytesMetrics(sshUploadPackServedBytes),
		ssh.WithReceivePackObjectsMetrics(sshReceivePackObjects),
	))
	gitalypb.RegisterSmartHTTPServiceServer(srv, smarthttp.NewServer(deps,
		smarthttp.WithPackfileNegotiationMetrics(smarthttpPackfileNegotiationMetrics),
		smarthttp.WithPackfileNegotiationDeepenMetrics(smarthttpPackfileNegotiationDeepenMetrics),
		smarthttp.WithUploadPackServedBytesMetrics(smarthttpUploadPackServedBytes),
		smarthttp.WithReceivePackObjectsMetrics(smarthttpReceivePackObjects),
	))
	gitalypb.RegisterConflictsServiceServer(srv, conflicts.NewServer(deps))
	gitalypb.RegisterRemoteServiceServer(srv, remote.NewServer(deps))
	gitalypb.RegisterServerServiceServer(srv, server.NewServer(deps))
	gitalypb.RegisterObjectPoolServiceServer(srv, objectpool.NewServer(deps))
	gitalypb.RegisterHookServiceServer(srv, hook.NewServer(deps))
	gitalypb.RegisterInternalGitalyServer(srv, internalgitaly.NewServer(deps))
	gitalypb.RegisterPartitionServiceServer(srv, partition.NewServer(deps))

	healthpb.RegisterHealthServer(srv, auth.UnauthenticatedHealthService{HealthServer: health.NewServer()})
	reflection.Register(srv)
	grpcprometheus.Register(srv)
}
```

**File:** proto/internal.proto (L10-12)
```text
// InternalGitaly is a gRPC service meant to be served by a Gitaly node, but
// only reachable by Praefect or other Gitalies
service InternalGitaly {
```

**File:** proto/internal.proto (L32-40)
```text
  // StorePoolMetadata stores repository-to-pool relationships into the
  // local pool metadata database. It receives a stream of (repo, pool) pairs
  // and merges them into the existing data.
  rpc StorePoolMetadata (stream StorePoolMetadataRequest) returns (StorePoolMetadataResponse) {
    option (op_type) = {
      op:          MUTATOR
      scope_level: STORAGE
    };
  }
```

**File:** proto/go/gitalypb/internal_grpc.pb.go (L29-35)
```go
// InternalGitalyClient is the client API for InternalGitaly service.
//
// For semantics around ctx use and closing/ending streaming RPCs, please refer to https://pkg.go.dev/google.golang.org/grpc/?tab=doc#ClientConn.NewStream.
//
// InternalGitaly is a gRPC service meant to be served by a Gitaly node, but
// only reachable by Praefect or other Gitalies
type InternalGitalyClient interface {
```

**File:** internal/gitaly/server/server_factory.go (L97-119)
```go
// CreateExternal creates a new external gRPC server. The external servers are closed
// before the internal servers when gracefully shutting down.
func (s *GitalyServerFactory) CreateExternal(secure bool, opts ...Option) (*grpc.Server, error) {
	server, err := s.New(true, secure, opts...)
	if err != nil {
		return nil, err
	}

	s.externalServers = append(s.externalServers, server)
	return server, nil
}

// CreateInternal creates a new internal gRPC server. Internal servers are closed
// after the external ones when gracefully shutting down.
func (s *GitalyServerFactory) CreateInternal(opts ...Option) (*grpc.Server, error) {
	server, err := s.New(false, false, opts...)
	if err != nil {
		return nil, err
	}

	s.internalServers = append(s.internalServers, server)
	return server, nil
}
```

**File:** internal/gitaly/service/hook/signal_post_receive_test.go (L35-46)
```go
	// Note: These tests will fail with PermissionDenied because they're not called via
	// Praefect's backchannel. This is the expected security behavior - the RPC should
	// only be callable from Praefect. Integration tests cover the full backchannel flow.

	t.Run("rejects call without backchannel", func(t *testing.T) {
		_, err := client.SignalPostReceiveReady(ctx, &gitalypb.SignalPostReceiveReadyRequest{
			StorageName:   storageName,
			TransactionId: 12345,
		})
		testhelper.RequireGrpcCode(t, err, codes.PermissionDenied)
		require.Contains(t, err.Error(), "can only be called via Praefect backchannel")
	})
```

**File:** internal/gitaly/service/internalgitaly/scan_pool_metadata.go (L18-26)
```go
func (s *server) ScanPoolMetadata(req *gitalypb.ScanPoolMetadataRequest, stream gitalypb.InternalGitaly_ScanPoolMetadataServer) error {
	ctx := stream.Context()
	storageName := req.GetStorageName()

	storagePath, err := s.locator.GetStorageByName(ctx, storageName)
	if err != nil {
		return structerr.NewInvalidArgument("get storage: %w", err)
	}

```

**File:** internal/cli/common/pool.go (L20-27)
```go
// ScanPoolMetadata calls the ScanPoolMetadata RPC and returns all repository-to-pool relationships.
func ScanPoolMetadata(ctx context.Context, client gitalypb.InternalGitalyClient, storageName string) ([]PoolMember, error) {
	stream, err := client.ScanPoolMetadata(ctx, &gitalypb.ScanPoolMetadataRequest{
		StorageName: storageName,
	})
	if err != nil {
		return nil, fmt.Errorf("scan pool metadata: %w", err)
	}
```
