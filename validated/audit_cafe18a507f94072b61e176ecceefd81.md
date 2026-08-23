## Vulnerability Analysis



### Title
Unbounded per-push reference-update count causes hook-subprocess and memory exhaustion in proc-receive handling - (File: internal/gitaly/hook/procreceive_handler.go)

### Summary
`NewProcReceiveHandler` reads an attacker/client-controlled number of reference-update pkt-lines from a `git push` and accumulates them without any limit before Gitaly processes each one, spawning a hook subprocess per update.

### Finding Description
When a client pushes to a repository whose `receive.procReceiveRefs` matches, `git-receive-pack(1)` invokes the proc-receive hook, which in Gitaly is implemented by `NewProcReceiveHandler`. This function loops over pkt-lines sent by the client and appends every parsed reference update to an unbounded slice: [1](#0-0) 

There is no cap on the number of updates a client may send in a single push — the loop runs until the client sends a flush packet, entirely under client control.

That accumulated slice is later consumed by `procReceiveHook`, which — for non-atomic operation — iterates the entire list and, for each individual update, invokes `receivePackReferenceUpdates`. In turn `receivePackReferenceUpdates` calls `hookManager.UpdateHook` (spawning the `update` hook, which is a subprocess or RPC round-trip to `gitaly-hooks`) once **per reference**, then queues it into `updateref.Updater`: [2](#0-1) [3](#0-2) 

Similarly, the sibling `UpdateReferences` RPC processes `request.GetUpdates()` per streamed message with no limit on the number of updates per message nor on the number of streamed messages, doing full validation (`git.ValidateReference`, `objectHash.ValidateHex`) for every entry: [4](#0-3) 

None of these paths impose a maximum reference-update count analogous to the pagination/`Limit` protections that Gitaly otherwise applies to read paths (e.g. `ListRefs`, `ListAllCommits`), which do enforce `PaginationParameter.Limit`: [5](#0-4) 

The `git/stats.ReceivePack` parser itself only samples the first 64KiB of the command list for statistics and explicitly does not enforce any protocol-level cap on command count: [6](#0-5) 

### Impact Explanation
An ordinary, authenticated-but-otherwise-unprivileged user who can push to a repository (a normal contributor/committer) can submit a single push containing a very large number of reference-update commands. This forces Gitaly to:
- Allocate an unbounded in-memory slice of `ReferenceUpdate` structs.
- Spawn (in the non-atomic proc-receive path) one hook subprocess/RPC per reference update, multiplying process creation, environment/payload construction, and I/O overhead by the attacker-chosen count.
- Consume CPU/memory on the Gitaly node handling that specific push, potentially starving other RPCs on the same node/repository and increasing latency or causing OOM under repeated/concurrent abuse.

This matches the "RPC-handler resource limits" DoS bug class named in scope: an unbounded per-request workload sized entirely by client input processed by a Gitaly RPC handler.

### Likelihood Explanation
Reachability requires only ordinary write/push access to a repository — no elevated privilege, leaked token, or malicious peer/MITM scenario is needed. Constructing the payload only requires emitting many `<old-oid> <new-oid> <ref>` pkt-lines within a single push's negotiation, well within reach of any git client or a simple scripted push.

### Recommendation
Impose a configurable maximum number of reference updates accepted per push/negotiation (and per `UpdateReferences` stream), rejecting excess with a clear `InvalidArgument`/`ResourceExhausted` error before allocation and before any hook subprocess is spawned. Consider also batching hook invocations (already partially done for the atomic path) instead of invoking the `update` hook once per reference in the non-atomic path, and enforce the same bound across all streamed `UpdateReferencesRequest` messages, not just per individual message.

### Proof of Concept
1. Create a test repository and configure `receive.procReceiveRefs` to route updates through the proc-receive hook (as used by Gitaly's `RegisterProcReceiveHook`).
2. Using a raw pkt-line writer, construct a push negotiation that sends the version/feature negotiation followed by a very large number (e.g. hundreds of thousands) of `<zero-oid> <new-oid> refs/heads/branch-N` lines before the terminating flush, as exercised structurally (at small scale) in `TestRegisterProcReceiveHook`'s "successful atomic multiple updates" case: [7](#0-6) 
3. Submit this via `git push` (or directly via the smarthttp/SSH receive-pack RPC) to a Gitaly instance.
4. Observe `NewProcReceiveHandler` accumulating all updates into memory, and — for the non-atomic case — `procReceiveHook` spawning one `update`-hook subprocess per reference, measurably degrading Gitaly's resource usage in proportion to the attacker-chosen update count, with no server-side cap rejecting the request.

### Citations

**File:** internal/gitaly/hook/procreceive_handler.go (L80-99)
```go
	var updates []ReferenceUpdate
	for scanner.Scan() {
		line := scanner.Bytes()

		// When all reference updates are transmitted, we expect a flush.
		if pktline.IsFlush(line) {
			break
		}

		data, err := pktline.Payload(line)
		if err != nil {
			return nil, nil, fmt.Errorf("receiving reference update: %w", err)
		}

		update, err := parseRefUpdate(data)
		if err != nil {
			return nil, nil, fmt.Errorf("parse reference update: %w", err)
		}
		updates = append(updates, update)
	}
```

**File:** internal/gitaly/hook/receivepack/receive_pack.go (L112-138)
```go
	} else {
		// Non-atomic reference updates are performed one at a time. Errors due to an update hook
		// failing are expected and should signal to the client it was rejected instead of
		// completely failing.
		for _, update := range handler.ReferenceUpdates() {
			if err := receivePackReferenceUpdates(
				ctx, cfg, req, repo, hookManager, []hook.ReferenceUpdate{update}, handler, handler,
			); err != nil {
				var (
					reason    string
					hookErr   hook.CustomHookError
					updateErr updateError
				)
				switch {
				case errors.As(err, &hookErr):
					reason = "update hook failed"
				case errors.As(err, &updateErr):
					reason = updateErr.Error()
				default:
					return fmt.Errorf("updating reference: %w", err)
				}

				rejectedUpdates[update.Ref] = reason
			} else {
				acceptedUpdates = append(acceptedUpdates, update)
			}
		}
```

**File:** internal/gitaly/hook/receivepack/receive_pack.go (L200-244)
```go
func receivePackReferenceUpdates(
	ctx context.Context,
	cfg config.Cfg,
	req gitcmd.ReceivePackRequest,
	repo *localrepo.Repo,
	hookManager hook.Manager,
	updates []hook.ReferenceUpdate,
	stdout, stderr io.Writer,
) (returnedErr error) {
	hooksPayload, err := setupHooksPayloadEnv(ctx, cfg, req, repo, gitcmd.UpdateHook)
	if err != nil {
		return fmt.Errorf("creating hooks payload: %w", err)
	}

	updater, err := updateref.New(ctx, repo, updateref.WithNoDeref())
	if err != nil {
		return fmt.Errorf("spawning ref updater: %w", err)
	}
	defer func() {
		if err := updater.Close(); err != nil && returnedErr == nil {
			returnedErr = fmt.Errorf("cancel ref updater: %w", err)
		}
	}()

	if err := updater.Start(); err != nil {
		return fmt.Errorf("start reference transaction: %w", err)
	}

	for _, update := range updates {
		if err := hookManager.UpdateHook(
			ctx,
			req.GetRepository(),
			update.Ref.String(),
			update.OldOID.String(),
			update.NewOID.String(),
			[]string{hooksPayload},
			stdout, stderr,
		); err != nil {
			return fmt.Errorf("running update hook: %w", err)
		}

		if err := updater.Update(update.Ref, update.NewOID, update.OldOID); err != nil {
			return fmt.Errorf("queueing ref to be updated: %w", err)
		}
	}
```

**File:** internal/gitaly/service/ref/update_references.go (L47-88)
```go
	for {
		// Only the first request may have its repository set.
		if request.GetRepository() != nil {
			return structerr.NewInvalidArgument("repository set in subsequent request")
		}

		if len(request.GetUpdates()) == 0 {
			return structerr.NewInvalidArgument("no updates specified")
		}

		for _, update := range request.GetUpdates() {
			reference := string(update.GetReference())
			if err := git.ValidateReference(reference); err != nil {
				return structerr.NewInvalidArgument("validating reference: %w", err).
					WithMetadata("reference", reference).
					WithDetail(&gitalypb.UpdateReferencesError{
						Error: &gitalypb.UpdateReferencesError_InvalidFormat{
							InvalidFormat: &gitalypb.InvalidRefFormatError{
								Refs: [][]byte{[]byte(reference)},
							},
						},
					})
			}

			// The old object ID may be empty, in which case we don't care about the current value of the
			// reference but instead do a force update of it.
			oldObjectID := string(update.GetOldObjectId())
			if len(oldObjectID) > 0 {
				if err := objectHash.ValidateHex(oldObjectID); err != nil {
					return structerr.NewInvalidArgument("validating old object ID: %w", err).WithMetadata("old_object_id", oldObjectID)
				}
			}

			newObjectID := string(update.GetNewObjectId())
			if err := objectHash.ValidateHex(newObjectID); err != nil {
				return structerr.NewInvalidArgument("validating new object ID: %w", err).WithMetadata("new_object_id", newObjectID)
			}

			if err := updater.Update(git.ReferenceName(reference), git.ObjectID(newObjectID), git.ObjectID(oldObjectID)); err != nil {
				return structerr.NewInvalidArgument("queueing update: %w", err)
			}
		}
```

**File:** internal/gitaly/service/ref/util.go (L499-522)
```go
func buildPaginationOpts(ctx context.Context, p *gitalypb.PaginationParameter) *paginationOpts {
	opts := &paginationOpts{}
	opts.IsPageToken = func(_ []byte) bool { return true }
	opts.Limit = math.MaxInt32

	if p == nil {
		return opts
	}

	if p.GetLimit() >= 0 {
		opts.Limit = int(p.GetLimit())
	}

	if p.GetPageToken() != "" {
		opts.IsPageToken = func(line []byte) bool {
			// Only use the first part of the line before \x00 separator
			if nullByteIndex := bytes.IndexByte(line, 0); nullByteIndex != -1 {
				line = line[:nullByteIndex]
			}

			return bytes.Equal(line, []byte(p.GetPageToken()))
		}
		opts.PageTokenError = true
	}
```

**File:** internal/git/stats/receive_pack.go (L18-22)
```go
// ReceivePackStatsPrefixSize is the number of leading bytes of a
// git-receive-pack request that are captured to extract push statistics. The
// command list and the packfile header sit at the very start of the request.
// A push whose command list is larger than this loses its statistics.
const ReceivePackStatsPrefixSize = 64 * 1024
```

**File:** internal/gitaly/hook/receivepack/receive_pack_test.go (L121-171)
```go
		{
			desc: "successful atomic multiple updates",
			setup: func() setupData {
				repo, repoPath := gittest.CreateRepository(t, ctx, cfg, gittest.CreateRepositoryConfig{
					SkipCreationViaService: true,
				})
				commitID := gittest.WriteCommit(t, cfg, repoPath)

				var pktLineRequest bytes.Buffer
				_, err := pktline.WriteString(&pktLineRequest, "version=1\000atomic")
				require.NoError(t, err)
				err = pktline.WriteFlush(&pktLineRequest)
				require.NoError(t, err)
				_, err = pktline.WriteString(&pktLineRequest, fmt.Sprintf("%s %s %s",
					gittest.DefaultObjectHash.ZeroOID, commitID.String(), "refs/heads/foo"))
				require.NoError(t, err)
				_, err = pktline.WriteString(&pktLineRequest, fmt.Sprintf("%s %s %s",
					gittest.DefaultObjectHash.ZeroOID, commitID.String(), "refs/heads/bar"))
				require.NoError(t, err)
				err = pktline.WriteFlush(&pktLineRequest)
				require.NoError(t, err)

				return setupData{
					repoProto:      repo,
					pktLineRequest: pktLineRequest.String(),
					updateHook: func(t *testing.T, _ context.Context, _ *gitalypb.Repository, ref, _, _ string, _ []string, stdout, _ io.Writer) error {
						_, err := fmt.Fprintf(stdout, "update hook: %s\n", ref)
						require.NoError(t, err)
						return nil
					},
					postReceiveHook: func(t *testing.T, _ context.Context, _ *gitalypb.Repository, _, _ []string, _ io.Reader, stdout, _ io.Writer) error {
						_, err := fmt.Fprintf(stdout, "post-receive hook\n")
						require.NoError(t, err)
						return nil
					},
					commit:         noopCommit,
					expectedStdout: "0014version=1\000atomic00000015ok refs/heads/foo0015ok refs/heads/bar0000",
					expectedStderr: "update hook: refs/heads/foo\nupdate hook: refs/heads/bar\npost-receive hook\n",
					expectedRefs: []git.Reference{
						{
							Name:   "refs/heads/foo",
							Target: commitID.String(),
						},
						{
							Name:   "refs/heads/bar",
							Target: commitID.String(),
						},
					},
				}
			},
		},
```
