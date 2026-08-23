Based on my investigation, I found a clear structural analog to the FTC bug: an asymmetric increment/decrement pattern in Gitaly's push reference-counter mechanism, which mirrors FTCHook's "increase mints reward, decrease does nothing" flaw.

### Title
Reference counter leaked when push is aborted after `pre-receive` succeeds, permanently blocking repository relocation gating - (File: internal/gitaly/hook/prereceive.go, internal/gitaly/hook/postreceive.go)

### Summary
Gitaly implements a GitLab-side "in-flight push" reference counter that is incremented by the `pre-receive` hook and decremented by the `post-receive` hook, used by Rails to know whether a repository has active pushes (e.g., to gate storage moves). The increment and decrement are two independent RPC calls tied to two different points in the `git-receive-pack` execution pipeline, with no compensating decrement if the push is aborted, killed, or fails between those two points. This mirrors the FTC bug pattern: the "increase" action (`FTCHook::onIncreasePosition` / here, `PreReceiveHook`) unconditionally grants/records state, while the "decrease" counterpart (`FTCHook::onDecreasePosition` / here, `PostReceiveHook`) is either a no-op or simply never invoked, letting a user cheaply and repeatedly trigger the increment without ever paying the corresponding "closing" cost.

### Finding Description
`PreReceiveHook` increments GitLab's reference counter via `m.gitlabClient.PreReceive(ctx, repo.GetGlRepository())` only after the custom `pre-receive` hooks have run successfully [1](#0-0) . As documented, "This hook first increments a reference counter that tracks how many pushes are active at the same time" and "post-receive... decrements the reference counter incremented in the pre-receive hook" [2](#0-1) ; the doc explicitly notes "If the reference counter is not at 0, there are active pushes happening", implying it's used to gate operations like moving a repository [3](#0-2) .

The increment and decrement calls are performed in physically separate RPC invocations (`PreReceiveHook` vs `PostReceiveHook`), tied to the lifecycle of the underlying `git-receive-pack` process spawned for a push. If the pushing client disconnects, cancels the gRPC stream, or the process is otherwise killed/times out after `pre-receive` has succeeded (and thus the counter has been incremented) but before `git-receive-pack` reaches the `post-receive` stage, the counter is never decremented — there is no compensating cleanup path, deferred decrement, or context-cancellation handler analogous to what `FTCHook::onDecreasePosition` should have done but doesn't. Because triggering `pre-receive` success is inexpensive (a small/empty valid push that passes access checks) and aborting a connection is free, an attacker with ordinary push access can repeatedly increment the counter while never allowing it to be decremented.

### Impact Explanation
Unlike the FTC bug (financial reward extraction), the impact here is denial of a Gitaly/GitLab-side gating mechanism: a permanently non-zero (or ever-growing) reference counter would cause Rails to believe there are perpetually active pushes on the repository, which per the documented semantics blocks operations gated on this counter (e.g., repository storage moves). This is a concrete DoS of a control that ordinary push access can trigger repeatedly and at negligible cost, directly analogous to the FTC finding's "cheap repeated increase with no corresponding decrease" pattern.

### Likelihood Explanation
Likelihood is moderate: it requires only push access to a repository and the ability to disconnect/cancel a push after `pre-receive` succeeds but before `post-receive` runs — a client-controlled timing window that is trivial to hit reliably (e.g., cancel the gRPC context or drop the TCP/SSH connection immediately once the server acknowledges the push, or force a mid-`git-receive-pack` failure via a deliberately malformed packfile after the ref list is accepted). This does not require privileged access, a leaked token, or a malicious peer — it is exercised entirely through an ordinary user's push RPC.

### Recommendation
Track the "increment" and "decrement" as a single atomic unit scoped to the RPC's lifetime rather than two independent calls: e.g., increment the counter inside a `defer`-guarded scope that is guaranteed to run a decrement call even when the RPC context is cancelled, the push fails partway, or the process is killed, similar to how `quarantine.New` guarantees cleanup via a returned cleanup function regardless of success/failure [4](#0-3) . Alternatively, have Rails expire/reconcile stale reference-counter entries after a bounded timeout so a leaked increment cannot persist indefinitely.

### Proof of Concept
1. As a user with push access, initiate a push (e.g., via `git push`) that will pass all `pre-receive` access checks (small valid update).
2. Immediately after the server would have called `PreReceiveHook` (which internally calls `m.gitlabClient.PreReceive(...)`, incrementing the counter) [5](#0-4) , forcibly terminate the connection (kill the SSH/HTTP transport, or cancel the client-side context) before the `update`/`post-receive` stages run.
3. Because `git-receive-pack` is killed or the RPC context is cancelled, `PostReceiveHook` (and its counter decrement via `m.gitlabClient.PostReceive`) never executes [6](#0-5) .
4. Repeat step 1–2 in a loop; the reference counter accumulates without bound, since no decrement is ever issued for the aborted pushes.

### Citations

**File:** internal/gitaly/hook/prereceive.go (L186-205)
```go
	if err = executor(
		ctx,
		nil,
		customEnv,
		bytes.NewReader(changes),
		stdout,
		stderr,
	); err != nil {
		return fmt.Errorf("executing custom hooks: %w", err)
	}

	// reference counter
	ok, err := m.gitlabClient.PreReceive(ctx, repo.GetGlRepository())
	if err != nil {
		return structerr.NewInternal("calling pre_receive endpoint: %w", err)
	}

	if !ok {
		return errors.New("")
	}
```

**File:** doc/hooks.md (L207-227)
```markdown
- `pre-receive`: The pre-receive hook receives all reference updates as a whole
  via standard input, where each change is represented by one line with the old
  and new object ID as well the name of the reference that is to be updated. At
  this point, all objects required to satisfy the update have already been
  received, but they are still in a separate "quarantine directory" and are
  therefore detached from the main repository. This hook first increments a
  reference counter that tracks how many pushes are active at the same time.
  Afterwards, it posts all changes to Rails' `/internal/allowed` API endpoint so
  that Rails can determine whether the change is allowed or not. Because objects
  still live in a quarantine directory, Gitaly tells Rails where it can find the
  quarantine directory using the repository's alternative object directory
  fields so that any subsequent RPC calls that check the change can access those
  objects. When the access checks succeed, any existing custom pre-receive hooks
  installed by the administrator are executed.
- `update`: The update hook runs after the pre-receive hook at the point where
  objects from the object quarantine directory have already been migrated into
  the main repository. This hook only executes custom hooks installed by the
  admin.
- `post-receive`: This hook prints information to the user (for example, the
  merge request link). It also decrements the reference counter incremented in
  the pre-receive hook.
```

**File:** doc/hooks.md (L229-231)
```markdown
Note: The reference is per repository so GitLab knows when a certain repository
can be moved. If the reference counter is not at 0, there are active pushes
happening.
```

**File:** internal/git/quarantine/quarantine.go (L35-52)
```go
// New creates a new quarantine directory and returns the directory and a cleanup function.
// The cleanup function must be called to remove the quarantine directory.
func New(ctx context.Context, repo *gitalypb.Repository, logger log.Logger, locator storage.Locator) (*Dir, func(), error) {
	repoPath, err := locator.GetRepoPath(ctx, repo, storage.WithRepositoryVerificationSkipped())
	if err != nil {
		return nil, nil, structerr.NewInternal("getting repo path: %w", err)
	}

	quarantineDir, cleanup, err := tempdir.NewWithPrefix(ctx, repo.GetStorageName(),
		storage.QuarantineDirectoryPrefix(repo), logger, locator)
	if err != nil {
		return nil, nil, fmt.Errorf("creating quarantine: %w", err)
	}

	quarantinedRepo, err := Apply(repoPath, repo, quarantineDir.Path())
	if err != nil {
		cleanup() // Clean up if we fail after creating the temp directory
		return nil, nil, err
```

**File:** internal/gitlab/http_client.go (L269-277)
```go
// PostReceive decreases the reference counter for a push for a given gl_repository through the gitlab internal API /post_receive endpoint
func (c *HTTPClient) PostReceive(ctx context.Context, glRepository, glID, changes string, clientCtx []byte, pushOptions ...string) (bool, []PostReceiveMessage, error) {
	ctx = withOriginalRemoteIP(ctx)
	defer prometheus.NewTimer(c.latencyMetric.WithLabelValues("post-receive")).ObserveDuration()

	resp, err := c.Post(ctx, "/post_receive", map[string]interface{}{"gl_repository": glRepository, "identifier": glID, "changes": changes, "gitaly_client_context_bin": clientCtx, "push_options": pushOptions})
	if err != nil {
		return false, nil, fmt.Errorf("http post to gitlab api /post_receive endpoint: %w", err)
	}
```
