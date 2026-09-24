This report doesn't apply to this repository. The reported bug is in `node/pkg/common` of `wormhole-foundation/wormhole`, an entirely separate blockchain-bridge project, not part of the Go language distribution mirrored here (`Kohvert/go--017`). There is no `node/pkg/common` path in this repository, and the wormhole codebase is not present here at all. [1](#0-0) 

I checked whether the Go runtime's own channel implementation has an analogous "check-then-send" race, since the report describes exactly that bug class (checking channel length/fullness, then sending, creating a TOCTOU race). The relevant code is the `chansend` fast path in `src/runtime/chan.go`: [2](#0-1) 

This is fundamentally different from the reported bug: `full(c)` here is a read used only to short-circuit a non-blocking send (`ch <- v` with `select`/`default`, i.e. `block == false`), and the code explicitly documents why the race is benign — [1](#0-0)  — if the fast-path observation is stale, control falls through to the locked slow path, which re-checks `c.qcount < c.dataqsiz` under `c.lock` before actually enqueueing [3](#0-2) . There is no separate unlocked "check length, then blindly send" pattern in this file that could actually block or misbehave the way the Wormhole bug did (where `len(channel) < cap` was checked outside a `select`, then an unconditional/blocking `channel <- x` was issued, letting a race turn a "safe" send into a block).

The Go project's own channel operations are single, atomic runtime primitives (`chansend`/`chanrecv`) guarded by an internal lock, not two independent user-level statements (`len()` check followed by a separate send statement), so the specific "TOCTOU between length check and send" defect class described in the report has no equivalent surface in `src/runtime/chan.go` or elsewhere in this repository's production code.

No vulnerability found for this question.

### Citations

**File:** src/runtime/chan.go (L189-207)
```go
	// Fast path: check for failed non-blocking operation without acquiring the lock.
	//
	// After observing that the channel is not closed, we observe that the channel is
	// not ready for sending. Each of these observations is a single word-sized read
	// (first c.closed and second full()).
	// Because a closed channel cannot transition from 'ready for sending' to
	// 'not ready for sending', even if the channel is closed between the two observations,
	// they imply a moment between the two when the channel was both not yet closed
	// and not ready for sending. We behave as if we observed the channel at that moment,
	// and report that the send cannot proceed.
	//
	// It is okay if the reads are reordered here: if we observe that the channel is not
	// ready for sending and then observe that it is not closed, that implies that the
	// channel wasn't closed during the first observation. However, nothing here
	// guarantees forward progress. We rely on the side effects of lock release in
	// chanrecv() and closechan() to update this thread's view of c.closed and full().
	if !block && c.closed == 0 && full(c) {
		return false
	}
```

**File:** src/runtime/chan.go (L214-242)
```go
	lock(&c.lock)

	if c.closed != 0 {
		unlock(&c.lock)
		panic(plainError("send on closed channel"))
	}

	if sg := c.recvq.dequeue(); sg != nil {
		// Found a waiting receiver. We pass the value we want to send
		// directly to the receiver, bypassing the channel buffer (if any).
		send(c, sg, ep, func() { unlock(&c.lock) }, 3)
		return true
	}

	if c.qcount < c.dataqsiz {
		// Space is available in the channel buffer. Enqueue the element to send.
		qp := chanbuf(c, c.sendx)
		if raceenabled {
			racenotify(c, c.sendx, nil)
		}
		typedmemmove(c.elemtype, qp, ep)
		c.sendx++
		if c.sendx == c.dataqsiz {
			c.sendx = 0
		}
		c.qcount++
		unlock(&c.lock)
		return true
	}
```
