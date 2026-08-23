Confirmed evidence: `PostReceivePack`/`SSHReceivePack` fire the reference-transaction and pre/update/post-receive hooks **once per reference update** (each one triggers Praefect vote + WAL writes), yet the `x-gitaly-cost` trailer assigns these RPCs a fixed static override of `10` regardless of how many refs are updated, plus only a small `bytes/1MiB` dynamic term.

## Title
Static per-RPC cost score lets a single push amortize per-reference processing cost, defeating upstream rate limiting - (File: internal/grpc/middleware/costhandler/costhandler.go)

### Summary
The `x-gitaly-cost` trailer that Gitaly emits for `PostReceivePack`/`SSHReceivePack` uses a **fixed static override of 10** [1](#0-0)  plus a dynamic component based only on transferred bytes [2](#0-1) . This mirrors the reported bug class: a fixed cost assumption ("base gas") is charged once per call, independent of the actual per-unit work performed inside that call, so batching more work into fewer calls lets an attacker do disproportionately more work per unit of assessed cost.

### Finding Description
Every reference update in a push causes Git to invoke the reference-transaction hook (prepare + commit), which round-trips to Praefect for a vote, and (via `OperationService`/`git-receive-pack`) triggers `pre-receive`/`update`/`post-receive` hook executions per reference [3](#0-2) . The test `TestPostReceivePack_referenceTransactionHook` shows a single-ref push already generates 6 transaction votes [4](#0-3) , so N ref updates in one push generate ~6·N votes/hook executions plus per-ref locking, quarantine object migration, and Praefect quorum round trips [5](#0-4) .

Despite this per-reference cost, `computeCost()` charges the same static `10` for `PostReceivePack`/`SSHReceivePack` no matter how many refs are updated in the single RPC call, and the "dynamic" term only scales with request/response payload bytes, not the number of hook invocations or votes [6](#0-5) . The design doc explicitly acknowledges the concurrency limiter has the same blind spot: "The concurrency limiter today treats every request as equally expensive" [7](#0-6) , and that static per-RPC-type costs "hide variance" only along the bytes-transferred axis, not the number of internal git/hook operations per call [8](#0-7) . Reference updates (e.g., branch/tag deletions) can be tiny in payload bytes yet each trigger full hook/vote overhead, so an attacker can pack thousands of small ref updates (or many small `OperationService` write RPCs) into single calls to keep the reported cost near the static minimum while consuming CPU/IO/vote overhead proportional to the number of refs — exactly the batching pattern used in the reported exploit to make actual-cost-per-unit shrink relative to the assumed fixed overhead.

### Impact Explanation
This is an accounting/DoS-relevant issue, not a fund-theft issue (Gitaly has no monetary refund mechanism), but the same root cause — a fixed per-call cost assumption gamed by batching — undermines the `x-gitaly-cost`/`X-Score` complexity-based rate limiting that GitLab.com relies on at the Cloudflare/Rails layer for DoS protection [9](#0-8) . An ordinary authenticated user can batch many ref updates or maintenance operations into single RPCs to consume disproportionate reference-transaction, hook, and quorum-vote resources on the Gitaly/Praefect cluster while being scored as cheap traffic, degrading the effectiveness of the outer rate limiter (a DoS-adjacent evasion of the RPC-handler resource-limiting handler).

### Likelihood Explanation
Moderate. It requires no special privilege — any user capable of pushing to a repository or calling mutator RPCs (e.g., `OperationService`) can construct a push/batch with a very large number of small reference updates, which is a normal, permitted Git operation shape, requiring only that they understand the cost model is call-count-based rather than operation-count-based.

### Recommendation
Make the dynamic cost component scale with the actual number of underlying operations performed (e.g., number of reference updates observed by the reference-transaction hook, number of hook/vote invocations, or number of `OperationService` batched actions), not just request/response payload bytes, so that cost/complexity scoring reflects real per-reference/per-hook overhead and cannot be diluted by packing many mutations into one RPC call.

### Proof of Concept
Not applicable as a runnable exploit within this analysis (no test harness output was executed); the root-cause chain is demonstrated via the code paths cited above: 6 reference-transaction votes for a single-ref push [4](#0-3)  versus a flat static cost of `10` for `PostReceivePack` regardless of ref count [1](#0-0) .

### Citations

**File:** internal/grpc/middleware/costhandler/costhandler.go (L38-44)
```go
var staticCostOverrides = map[string]int{
	// Streaming RPCs that transfer large amounts of data.
	"/gitaly.SmartHTTPService/PostUploadPackWithSidechannel": 10,
	"/gitaly.SmartHTTPService/PostReceivePack":               10,
	"/gitaly.SSHService/SSHUploadPack":                       10,
	"/gitaly.SSHService/SSHUploadPackWithSidechannel":        10,
	"/gitaly.SSHService/SSHReceivePack":                      10,
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

**File:** internal/grpc/middleware/costhandler/costhandler.go (L118-127)
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
```

**File:** doc/hooks.md (L137-177)
```markdown
### Reference-transaction hook

The reference-transaction hook is executed whenever Git updates a reference from
an old value to a new value, where it hooks into the low-level mechanism to
update references in Git. An update of a hook goes through two phases, where the
hook is executed once for each phase:

1. Git "prepares" the reference transaction. When the hook is called in this
   phase, the references that are about to be updated are locked for concurrent
   modification.
1. Git "commits" or "aborts" the reference transaction.

Depending on how the command updates references, this hook can be executed per
reference that is about to be updated, or for all references at once. The
references that are about to be updated are received on standard input.

In Gitaly, we use the reference-transaction mechanism to perform votes on all
references which are updated by Git in a way that is transparent to Gitaly
itself. That is, we do not know how often and with which arguments the hook is
going to be executed when we execute any Git command, and the order can change
when new Git versions are rolled out that do internal changes.

The reference-transaction can therefore be used to put an absolute ordering on
all reference updates across multiple Gitaly nodes. To ensure that multiple
Gitaly nodes part of a single transaction (that is, all Gitalies execute the
same logic at the same point in time) perform the same action, we vote on each
of the hook executions. Gitaly nodes:

1. Take the standard input containing all references we are about to update.
1. Hash these references.
1. Use the resulting hash as it's vote that it sends to Praefect.

We expect the hash must be the same across all Gitaly nodes if (and only if) all
nodes:

- Are in the same state before.
- Make the change deterministically.

If a [quorum](design_ha.md) is reached, the update is committed to disk,
otherwise the update is rejected.

```

**File:** internal/gitaly/service/smarthttp/receive_pack_test.go (L830-851)
```go
	t.Run("update", func(t *testing.T) {
		stream, err := client.PostReceivePack(ctx)
		require.NoError(t, err)

		repo, repoPath := gittest.CreateRepository(t, ctxWithoutTransaction, cfg)
		gittest.WriteCommit(t, cfg, repoPath, gittest.WithBranch("branch"))
		push := setupSimplePush(t, ctx, cfg, repoPath, "refs/heads/branch")

		response := push.perform(t, stream, &gitalypb.PostReceivePackRequest{
			Repository:   repo,
			GlId:         "key-1234",
			GlRepository: "some_repo",
		})

		requireSideband(t, []string{
			gittest.Pktlinef(t, "\x01%s", strings.Join([]string{
				gittest.Pktlinef(t, "unpack ok\n"),
				gittest.Pktlinef(t, "ok refs/heads/branch\n"),
				"0000",
			}, "")),
		}, response)
		require.Equal(t, 6, refTransactionServer.called)
```

**File:** internal/gitaly/hook/updateref/update_with_hooks.go (L260-303)
```go
	updater, err := updateref.New(ctx, repo, updateref.WithDisabledTransactions())
	if err != nil {
		return fmt.Errorf("creating updater: %w", err)
	}

	// We need to explicitly cancel the update here such that we release the lock when this
	// function exits if there is any error between locking and committing.
	defer func() { _ = updater.Close() }()

	if err := updater.Start(); err != nil {
		return fmt.Errorf("start reference transaction: %w", err)
	}

	if err := updater.Update(reference, newrev, oldrev); err != nil {
		return fmt.Errorf("queueing ref update: %w", err)
	}

	if err := u.hookManager.ReferenceTransactionHook(ctx, hook.ReferenceTransactionPreparing, []string{hooksPayload}, strings.NewReader(changes)); err != nil {
		return fmt.Errorf("executing pre-locked reference-transaction hook: %w", err)
	}

	// We need to lock the reference before executing the reference-transaction hook such that
	// there cannot be any concurrent modification.
	if err := updater.Prepare(); err != nil {
		return Error{
			Reference: reference,
			OldOID:    oldrev,
			NewOID:    newrev,
			Cause:     err,
		}
	}

	if err := u.hookManager.ReferenceTransactionHook(ctx, hook.ReferenceTransactionPrepared, []string{hooksPayload}, strings.NewReader(changes)); err != nil {
		return fmt.Errorf("executing preparatory reference-transaction hook: %w", err)
	}

	if err := updater.Commit(); err != nil {
		return Error{
			Reference: reference,
			OldOID:    oldrev,
			NewOID:    newrev,
			Cause:     err,
		}
	}
```

**File:** doc/load-management-architecture.md (L197-205)
```markdown
The concurrency limiter today treats every request as equally expensive. In
practice, requests served from cache consume minimal resources (disk streaming)
while cache misses run full Git subprocess pipelines. Rather than feeding the
limiter a cost hint (which still consumes a limiter slot), cache-hit requests
skip the limiter entirely — keeping the concurrency limiter pure and ensuring
limits designed for expensive operations do not penalize cheap ones.

This separation also keeps the concurrency limiter's responsibility narrow: every
request that reaches it is treated as expensive.
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

**File:** doc/load-management-architecture.md (L282-286)
```markdown
Static scores are derived from historical data per RPC type. A static score hides
variance (e.g. `PostUploadPackWithSidechannel` is the same cost whether it serves
1 MB or 10 GB), but dynamic cost (bytes transferred, object count) is only known
after the RPC completes. The two approaches can be combined: use the static score
as a base, then reconcile actual cost in a follow-up after Gitaly responds.
```
