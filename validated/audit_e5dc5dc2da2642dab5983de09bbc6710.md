Now I have enough to identify the analog. The `costhandler` package's static cost model treats every mutator RPC as a single fixed unit (e.g., `defaultMutatorCost = 5`) regardless of how much actual server-side work the RPC performs internally, and the only variable component (`dynamicCostFromContext`) is derived purely from wire-level payload bytes, not from the number of expensive git operations, hooks, or actions actually executed.### Title
Cost-based rate-limiting bypass via unbounded per-message work in `UserCommitFiles` — actions never accounted for in `x-gitaly-cost` (File: internal/grpc/middleware/costhandler/costhandler.go)

### Summary
Gitaly's `x-gitaly-cost` trailer, computed by `costhandler.computeCost`, is the value Rails/Workhorse forward as the `X-Score` header to drive Cloudflare's complexity-based rate limiting [1](#0-0) . For any given RPC, this cost is a fixed per-method static weight (`defaultMutatorCost = 5` for all mutator RPCs, unless overridden) plus a component derived solely from wire-level payload bytes [2](#0-1) [3](#0-2) . For streaming mutator RPCs such as `UserCommitFiles`, a client can send an unbounded number of expensive actions (blob writes, moves, chmods, directory creates) in a single stream while keeping the wire bytes minimal, so the reported cost stays flat regardless of how much server-side work (git object writes, hook executions) is actually triggered.

### Finding Description
This mirrors the underlying bug class from the ZetaChain report: a resource-accounting mechanism that is supposed to reflect real per-message/per-operation cost instead collapses to a fixed value regardless of how many expensive operations are packed into a single request, letting an unprivileged caller do a large amount of paid-for-once work.

`UserCommitFiles` accepts a stream of `UserCommitFilesRequest` messages: one header followed by an arbitrary number of `action` messages, each of which can create/update/move/delete a blob or create a directory [4](#0-3) . The handler loops over every action to write blobs and mutate the in-memory tree with no upper bound on the number of actions per stream [5](#0-4) [6](#0-5) . Each `CREATE`/`UPDATE`/`MOVE` action independently invokes `quarantineRepo.WriteBlob`, and the final result runs `treeEntry.Write` and `quarantineRepo.WriteCommit`, all git-object operations with real CPU/IO cost.

The `x-gitaly-cost` trailer set for this RPC comes from `computeCost`, which sums:
1. a static weight based purely on the RPC's `op_type` (`OpMutator` → `defaultMutatorCost = 5`, since `UserCommitFiles` has no entry in `staticCostOverrides`) [7](#0-6) [8](#0-7) 
2. a dynamic component computed only from `InPayloadBytes + OutPayloadBytes` divided by 1 MiB [9](#0-8) 

Because action headers (`UserCommitFilesActionHeader`) are tiny protobuf messages and content can be empty or trivially small (e.g. `CREATE_DIR`, `CHMOD`, or `DELETE` actions carry no file content at all), a client can chain thousands of such actions in one `UserCommitFiles` stream while the aggregate wire bytes remain far below the 1 MiB `byteCostDivisor`. The resulting cost score is therefore the same flat `5` regardless of whether the stream contains one action or ten thousand — exactly analogous to the ZetaChain bug where the gas meter is reset per message, so only the last message's cost is billed regardless of how many `MsgGasPriceVoter` messages were bundled into the transaction.

### Impact Explanation
This breaks the "Cost-Aware Admission"/"RPC Cost Score" layer that Gitaly's own architecture doc describes as the mechanism by which Rails/Workhorse and Cloudflare rate-limit expensive callers [10](#0-9) . A single unprivileged, authenticated user (any user who can call `UserCommitFiles`, which is explicitly documented as handling "untrusted" user operations [11](#0-10) ) can repeatedly submit streams containing very many low-content actions, forcing Gitaly to perform large numbers of blob writes, tree mutations, and a final commit/hook execution, while consistently reporting a minimal, static cost score. Because the external Cloudflare/Rails rate limiter trusts this score as the primary signal of RPC expense, the attacker's requests are systematically under-priced relative to actual resource consumption, allowing sustained DoS-style load on git object writing and hook execution — filling Gitaly's actual processing capacity — without tripping the complexity-based rate limits that are supposed to catch exactly this kind of abuse.

### Likelihood Explanation
Likelihood is high for any deployment relying on the `x-gitaly-cost`/`X-Score` mechanism as an actual throttling signal, since:
- `UserCommitFiles` is a normal, authenticated, non-privileged git write path (any user with commit access).
- No server-side limit exists on the number of actions per `UserCommitFiles` stream (confirmed by `pbActions` being appended without bound) [12](#0-11) .
- The dynamic cost component depends only on wire bytes, which is trivial to keep low per action (e.g., `CREATE_DIR`/`CHMOD`/`DELETE` carry no content payload).
- This is a documented, currently-shipped mechanism (`doc/load-management-architecture.md`) intended specifically to prevent this class of abuse, so the gap is directly exploitable against the stated security control.

### Recommendation
Do not derive the dynamic cost component from wire-level payload bytes alone for streaming mutator RPCs. Instead, account for the number of discrete server-side operations actually performed (e.g., number of actions processed by `userCommitFiles`, blob writes, or hook invocations), and add this as a per-operation cost contribution similar to how the ZetaChain fix recommends not resetting the gas meter per message but instead accumulating cost across all units of work in a request. Concretely, track an operation counter in context (analogous to `grpcstats.PayloadBytesStats`) that `commit_files.go` increments once per action, and fold that count into `computeCost` for `UserCommitFiles` and any other streaming mutator RPC with a variable-length, attacker-controlled repeated field (e.g., `UserApplyPatch`, `UserRebaseConfirmable`).

### Proof of Concept
1. Establish a `UserCommitFiles` stream against a repository the attacker has write access to.
2. Send one `UserCommitFilesRequestHeader` message.
3. Send N (e.g. 50,000) `UserCommitFilesAction` messages, each a `CREATE_DIR` or `CHMOD` header with no content payload — each individual action message is tens of bytes, so total `InPayloadBytes` stays well under 1 MiB.
4. Close the stream and observe the `x-gitaly-cost` trailer returned by the `StreamInterceptor` — it will remain `5` (the static `defaultMutatorCost`) exactly as it would for a single-action request, per `computeCost`'s logic [3](#0-2) , even though the server performed 50,000 tree mutations and a final `WriteCommit`/hook execution internally [13](#0-12) .
5. Repeat this request continuously; the external rate limiter, relying on the reported cost score, will not distinguish this from cheap, minimal `UserCommitFiles` calls, allowing sustained resource consumption on the Gitaly node without triggering complexity-based throttling.

### Citations

**File:** internal/grpc/middleware/costhandler/costhandler.go (L1-9)
```go
// Package costhandler is a Signal Export layer component that reads per-RPC
// resource data and sets the x-gitaly-cost gRPC response trailer.
//
// The cost score combines a static weight for the RPC type with the dynamic
// bytes transferred. Clients (Rails, Workhorse) use this for rate-limit
// budget accounting and forward it as the X-Score HTTP header for Cloudflare.
//
// See doc/load-management-architecture.md for the full design.
package costhandler
```

**File:** internal/grpc/middleware/costhandler/costhandler.go (L25-34)
```go
const (
	defaultAccessorCost    = 1
	defaultMutatorCost     = 5
	defaultMaintenanceCost = 3
	defaultUnknownCost     = 1
)

// byteCostDivisor controls how payload bytes contribute to the cost score.
// Every byteCostDivisor bytes adds 1 to the cost.
const byteCostDivisor = 1 << 20 // 1 MiB
```

**File:** internal/grpc/middleware/costhandler/costhandler.go (L36-58)
```go
// staticCostOverrides allows per-RPC cost overrides for RPCs that are known to
// be especially cheap or expensive relative to their operation type default.
var staticCostOverrides = map[string]int{
	// Streaming RPCs that transfer large amounts of data.
	"/gitaly.SmartHTTPService/PostUploadPackWithSidechannel": 10,
	"/gitaly.SmartHTTPService/PostReceivePack":               10,
	"/gitaly.SSHService/SSHUploadPack":                       10,
	"/gitaly.SSHService/SSHUploadPackWithSidechannel":        10,
	"/gitaly.SSHService/SSHReceivePack":                      10,

	// Large-object enumeration or diff operations.
	"/gitaly.DiffService/CommitDiff":    8,
	"/gitaly.DiffService/RawDiff":       8,
	"/gitaly.DiffService/RawPatch":      8,
	"/gitaly.BlobService/GetBlobs":      6,
	"/gitaly.BlobService/ListBlobs":     6,
	"/gitaly.CommitService/ListCommits": 6,

	// Lightweight RPCs.
	"/gitaly.RefService/FindDefaultBranchName":   1,
	"/gitaly.RepositoryService/RepositoryExists": 1,
	"/gitaly.ServerService/ServerInfo":           0,
}
```

**File:** internal/grpc/middleware/costhandler/costhandler.go (L84-91)
```go
// computeCost returns the cost score for a completed RPC. It combines a static
// base cost for the RPC type with a dynamic component from actual bytes
// transferred, read from the RPCEntry in context.
func computeCost(ctx context.Context, fullMethod string) int {
	static := staticCostForMethod(fullMethod)
	dynamic := dynamicCostFromContext(ctx)
	return static + dynamic
}
```

**File:** internal/grpc/middleware/costhandler/costhandler.go (L93-116)
```go
// staticCostForMethod returns the static cost weight for the given full method
// name. It first checks per-RPC overrides, then falls back to a default based
// on the method's operation type from the proto registry.
func staticCostForMethod(fullMethod string) int {
	if cost, ok := staticCostOverrides[fullMethod]; ok {
		return cost
	}

	methodInfo, err := protoregistry.GitalyProtoPreregistered.LookupMethod(fullMethod)
	if err != nil {
		return defaultUnknownCost
	}

	switch methodInfo.Operation {
	case protoregistry.OpAccessor:
		return defaultAccessorCost
	case protoregistry.OpMutator:
		return defaultMutatorCost
	case protoregistry.OpMaintenance:
		return defaultMaintenanceCost
	default:
		return defaultUnknownCost
	}
}
```

**File:** internal/grpc/middleware/costhandler/costhandler.go (L118-128)
```go
// dynamicCostFromContext computes the dynamic cost contribution from payload
// bytes tracked by the grpcstats.PayloadBytes stats handler.
func dynamicCostFromContext(ctx context.Context) int {
	stats := grpcstats.PayloadBytesStatsFromContext(ctx)
	if stats == nil {
		return 0
	}
	totalBytes := stats.InPayloadBytes + stats.OutPayloadBytes

	return int(math.Ceil(float64(totalBytes) / float64(byteCostDivisor)))
}
```

**File:** internal/gitaly/service/operations/commit_files.go (L478-494)
```go
	for _, action := range actions {
		if err = applyAction(
			ctx,
			action,
			treeEntry,
			quarantineRepo,
		); err != nil {
			return "", fmt.Errorf("performing action %T: %w", action, err)
		}
	}

	if err := treeEntry.Write(
		ctx,
		quarantineRepo,
	); err != nil {
		return "", fmt.Errorf("writing tree %w", err)
	}
```

**File:** internal/gitaly/service/operations/commit_files.go (L604-635)
```go
	type action struct {
		header  *gitalypb.UserCommitFilesActionHeader
		content []byte
	}

	var pbActions []action

	for {
		req, err := stream.Recv()
		if err != nil {
			if errors.Is(err, io.EOF) {
				break
			}

			return fmt.Errorf("receive request: %w", err)
		}

		switch payload := req.GetAction().GetUserCommitFilesActionPayload().(type) {
		case *gitalypb.UserCommitFilesAction_Header:
			pbActions = append(pbActions, action{header: payload.Header})
		case *gitalypb.UserCommitFilesAction_Content:
			if len(pbActions) == 0 {
				return errors.New("content sent before action")
			}

			// append the content to the previous action
			content := &pbActions[len(pbActions)-1].content
			*content = append(*content, payload.Content...)
		default:
			return fmt.Errorf("unhandled action payload type: %T", payload)
		}
	}
```

**File:** internal/gitaly/service/operations/commit_files.go (L637-714)
```go
	actions := make([]commitAction, 0, len(pbActions))
	for _, pbAction := range pbActions {
		if _, ok := gitalypb.UserCommitFilesActionHeader_ActionType_name[int32(pbAction.header.GetAction())]; !ok {
			return structerr.NewInvalidArgument("NoMethodError: undefined method `downcase' for %d:Integer", pbAction.header.GetAction())
		}

		path, err := validatePath(repoPath, string(pbAction.header.GetFilePath()))
		if err != nil {
			return structerr.NewInvalidArgument("validate path: %w", err)
		}

		content := io.Reader(bytes.NewReader(pbAction.content))
		if pbAction.header.GetBase64Content() {
			content = base64.NewDecoder(base64.StdEncoding, content)
		}

		switch pbAction.header.GetAction() {
		case gitalypb.UserCommitFilesActionHeader_CREATE:
			blobID, err := quarantineRepo.WriteBlob(ctx, content, localrepo.WriteBlobConfig{
				Path: path,
			})
			if err != nil {
				return fmt.Errorf("write created blob: %w", err)
			}

			actions = append(actions, createFile{
				OID:            blobID.String(),
				Path:           path,
				ExecutableMode: pbAction.header.GetExecuteFilemode(),
			})
		case gitalypb.UserCommitFilesActionHeader_CHMOD:
			actions = append(actions, changeFileMode{
				Path:           path,
				ExecutableMode: pbAction.header.GetExecuteFilemode(),
			})
		case gitalypb.UserCommitFilesActionHeader_MOVE:
			prevPath, err := validatePath(repoPath, string(pbAction.header.GetPreviousPath()))
			if err != nil {
				return structerr.NewInvalidArgument("validate previous path: %w", err)
			}

			var oid git.ObjectID
			if !pbAction.header.GetInferContent() {
				var err error
				oid, err = quarantineRepo.WriteBlob(ctx, content, localrepo.WriteBlobConfig{
					Path: path,
				})
				if err != nil {
					return err
				}
			}
			actions = append(actions, moveFile{
				Path:    prevPath,
				NewPath: path,
				OID:     oid.String(),
			})
		case gitalypb.UserCommitFilesActionHeader_UPDATE:
			oid, err := quarantineRepo.WriteBlob(ctx, content, localrepo.WriteBlobConfig{
				Path: path,
			})
			if err != nil {
				return fmt.Errorf("write updated blob: %w", err)
			}

			actions = append(actions, updateFile{
				Path: path,
				OID:  oid.String(),
			})
		case gitalypb.UserCommitFilesActionHeader_DELETE:
			actions = append(actions, deleteFile{
				Path: path,
			})
		case gitalypb.UserCommitFilesActionHeader_CREATE_DIR:
			actions = append(actions, createDirectory{
				Path: path,
			})
		}
	}
```

**File:** doc/load-management-architecture.md (L269-280)
```markdown
### RPC Cost Score (`x-gitaly-cost`)

Gitaly returns a cost score for each RPC as a gRPC response trailer.
Gitaly has the most context about the actual cost of each RPC, making it the right
place to own this value. Rails and Workhorse translate the `x-gitaly-cost`
trailer into an `X-Score` HTTP response header, making the cost signal
available to any upstream rate limiter.

> On GitLab.com, the `X-Score` header feeds Cloudflare's
> [complexity-based rate limiting](https://developers.cloudflare.com/waf/rate-limiting-rules/request-rate/#complexity-based-rate-limiting).
> Self-managed deployments can use the same header with any upstream
> rate limiter or ignore it.
```

**File:** proto/go/gitalypb/operations_grpc.pb.go (L44-47)
```go
// OperationService provides an interface for performing mutating git
// operations on a repository on behalf of a user. The user's operation is
// treated as untrusted. Any reference update is thus checked against GitLab's
// '/allowed' endpoint.
```
