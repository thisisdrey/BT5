### Title
Concurrency-limiter queue documented as FIFO is actually LIFO, enabling request starvation / DoS analogous to the FILO deposit-queue bug - (File: internal/limiter/resizable_semaphore.go)

### Summary
Gitaly's per-key concurrency limiter (used for `[[concurrency]]` RPC/per-repo/pack-objects backpressure) queues waiting callers in a `resizableSemaphore` that is documented as FIFO but is implemented as a stack (LIFO), mirroring the "FILO dequeue" root cause described in the external report.

### Finding Description
`resizableSemaphore.waiters` is a `container/list.List` explicitly commented as a "FIFO list of waiters" [1](#0-0) . However, `Acquire` inserts new waiters at the **front** of the list with `PushFront`, and `notifyWaiters` always pops from the **front** (`s.waiters.Front()`) when a slot frees up: [2](#0-1) [3](#0-2) 

Since the most recently queued waiter is placed at the front and freed slots are always handed to the front-most waiter, the semaphore behaves as last-in-first-out: newly arriving callers are serviced ahead of callers that have been waiting longer. This is the exact same class of bug as the Sherlock report — a stack used where FIFO fairness is required/assumed, causing early entries to be perpetually deprioritized as new entries keep arriving.

This `resizableSemaphore` is the concrete semaphore type backing both `concurrencyTokens` and `queueTokens` of `keyedConcurrencyLimiter`, as shown by the type assertions in `NewConcurrencyLimiter`'s resize callback: [4](#0-3) . `keyedConcurrencyLimiter.acquire` calls `sem.concurrencyTokens.Acquire(ctx)` after entering the queue [5](#0-4) , meaning production RPC concurrency limiting (`[[concurrency]] max_per_repo`, `max_queue_wait`, `max_queue_size`, and the dedicated pack-objects limiter `TypePackObjects`) relies on this ordering. This limiter gates ordinary user-triggered RPCs such as `PostUploadPackWithSidechannel` (clones/fetches) per the documented example [6](#0-5) .

### Impact Explanation
When a repository/RPC's concurrency limit is saturated and a queue is configured (`max_queue_size`, `max_queue_wait`), legitimate requests that queued first can be continuously overtaken by newer requests arriving from any client hitting the same limiting key (same repo + RPC). Under sustained load — e.g., a client or attacker repeatedly issuing requests against a hot repository — early, legitimate requests can be starved until they exceed `max_queue_wait` and are rejected with `RESOURCE_EXHAUSTED` (`ErrMaxQueueTime`), even though slots were becoming available and being consumed by later arrivals the entire time. This is a genuine DoS of the RPC-handler's queue fairness guarantee: an ordinary user (or a bursty but non-malicious client) can cause other users' requests to the same repository to be denied service purely by continuously refilling the front of the queue, which is exactly the "some deposits/requests never get processed" failure mode described in the analog report.

### Likelihood Explanation
Reachable by any client issuing ordinary Git operations (clone/fetch/push RPCs) against a repository that is configured with `[[concurrency]]` limits and has more than one request queued at a time — a common, low-privilege scenario needing no token leakage, MITM, or malicious peer/node involvement. The more requests queued concurrently (busy repo, low `max_per_repo`), the more consistently starvation manifests, since every new arrival is placed ahead of already-waiting callers.

### Recommendation
Make `resizableSemaphore.waiters` behave as true FIFO: insert new waiters with `PushBack` instead of `PushFront` (keeping `notifyWaiters` popping from `Front()`), or conversely keep `PushFront` and change `notifyWaiters`/`stopWaiter` to operate on `Back()`. Add a regression test that queues N waiters in order and asserts they are woken up in the exact order they were enqueued as slots become available (currently `resizable_semaphore_test.go` doesn't assert ordering across waiters, only aggregate acquire/release counts).

### Proof of Concept
1. Configure `[[concurrency]]` with `max_per_repo = 1`, `max_queue_size = N`, `max_queue_wait` reasonably large for a given RPC.
2. Client A sends a slow request that occupies the single concurrency token.
3. Client B sends request #1, which queues (`PushFront` → element B1 is Front).
4. Client C sends request #2 shortly after, which queues (`PushFront` → element C1 becomes new Front, pushing B1 behind it).
5. Repeat step 4 with more clients (D, E, …) continuously arriving before A's request finishes.
6. When A's request finishes and calls `Release()` → `notifyWaiters()`, the semaphore hands the token to `s.waiters.Front()`, which is the most recently added waiter (e.g., the last client), not B1 which arrived first.
7. If new arrivals keep coming faster than slots free up, B1 (and any early waiter) can be pushed behind the front indefinitely until its context timeout (`max_queue_wait`) fires, returning `ErrMaxQueueTime`/`RESOURCE_EXHAUSTED` — the analog of "User deposit may never be entertained from deposit queue." [7](#0-6) [3](#0-2)

### Citations

**File:** internal/limiter/resizable_semaphore.go (L35-37)
```go
	// waiters is a FIFO list of waiters waiting for the resource.
	waiters *list.List
}
```

**File:** internal/limiter/resizable_semaphore.go (L53-82)
```go
// Acquire allows the caller to acquire the semaphore. If the semaphore is full, the caller is blocked until there
// is an available slot or the context is canceled or the context exceeds the deadline. If the context exceeds the
// deadline, ErrMaxQueueTime is returned. If the context is canceled, context's error is returned. Otherwise,
// this function returns nil after acquired.
func (s *resizableSemaphore) Acquire(ctx context.Context) error {
	s.Lock()
	if s.count() < s.size {
		select {
		case <-ctx.Done():
			s.Unlock()
			return s.contextError(ctx)
		default:
			s.current++
			s.Unlock()
			return nil
		}
	}

	w := &waiter{ready: make(chan struct{})}

	element := s.waiters.PushFront(w)
	s.Unlock()

	select {
	case <-ctx.Done():
		return s.stopWaiter(element, w, s.contextError(ctx))
	case <-w.ready:
		return nil
	}
}
```

**File:** internal/limiter/resizable_semaphore.go (L119-137)
```go
// notifyWaiters scans from the head of the s.waiters linked list, removing waiters until there are no free slots. This
// function must only be called after the mutex of s is acquired.
func (s *resizableSemaphore) notifyWaiters() {
	for {
		element := s.waiters.Front()
		if element == nil {
			break
		}

		if s.count() >= s.size {
			return
		}

		w := element.Value.(*waiter)
		s.current++
		s.waiters.Remove(element)
		close(w.ready)
	}
}
```

**File:** internal/limiter/concurrency_limiter.go (L93-107)
```go
	sem.monitor.Queued(ctx, limitingKey, sem.queueLength())
	defer sem.monitor.Dequeued(ctx)

	if sem.maxQueueWait != 0 {
		if sem.setWaitTimeoutContext != nil {
			ctx = sem.setWaitTimeoutContext()
		} else {
			var cancel context.CancelFunc
			ctx, cancel = context.WithTimeout(ctx, sem.maxQueueWait)
			defer cancel()
		}
	}

	// Try to acquire the concurrency token now that we're in the queue.
	return sem.concurrencyTokens.Acquire(ctx)
```

**File:** internal/limiter/concurrency_limiter.go (L171-184)
```go
	// When the capacity of the limiter is updated we also need to update the size of both the queuing tokens as
	// well as the concurrency tokens to match the new size.
	limit.AfterUpdate(func(val int) {
		for _, keyedLimiter := range limiter.limitsByKey {
			if keyedLimiter.queueTokens != nil {
				if semaphore, ok := keyedLimiter.queueTokens.(*resizableSemaphore); ok {
					semaphore.Resize(uint(val + limiter.maxQueueLength))
				}
			}
			if semaphore, ok := keyedLimiter.concurrencyTokens.(*resizableSemaphore); ok {
				semaphore.Resize(uint(val))
			}
		}
	})
```

**File:** doc/backpressure.md (L17-24)
```markdown
Limit the number of concurrent RPCs that are in flight on each Gitaly node for each
repository per RPC using `[[concurrency]]` configuration:

```toml
[[concurrency]]
rpc = "/gitaly.SmartHTTPService/PostUploadPackWithSidechannel"
max_per_repo = 1
```
```
