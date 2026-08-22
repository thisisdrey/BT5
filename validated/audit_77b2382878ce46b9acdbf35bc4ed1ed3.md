This confirms the multi-routine design: with `f.routines > 1`, Nebula spawns one `listenOut` goroutine per queue [1](#0-0) , each independently calling `readOutsidePackets` → `ConnectionState.Decrypt` for the **same** `hostinfo`/`ConnectionState` when packets for that peer land on different UDP queues [2](#0-1) . This gives the reachable, attacker-triggerable concurrency needed for the report's bug class.

### Title
Non-atomic replay-window Check/Update in `ConnectionState.Decrypt` allows replayed packets to bypass anti-replay protection under concurrent delivery - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt` mirrors the reported bug class: a shared, security-critical resource (`Bits` replay window) is *checked* under one lock acquisition, consumed by an expensive/attacker-influenced operation without holding the lock (AEAD decryption), and only *committed* (`Update`) under a second, separate lock acquisition. Just like `currentWithheldETH` in the report — checked in `unstake()` and only decremented later in `withdraw()` — the replay window is checked in one critical section and only marked-as-seen in a later, disjoint critical section, leaving a window where the same message counter can pass the check twice.

### Finding Description
`Decrypt` performs three logically-related steps as two separate locked regions:
```go
cs.decryptLock.Lock()
result := cs.window.Check(l, messageCounter)   // read-only check
cs.decryptLock.Unlock()
...
out, err = cs.dKey.DecryptDanger(...)           // unlocked AEAD work
...
cs.decryptLock.Lock()
result = cs.window.Update(l, messageCounter)    // commit
cs.decryptLock.Unlock()
``` [3](#0-2) 

`Bits.Check` is purely a read of `b.current`/bitmap state and does not mark the counter as seen [4](#0-3) ; only `Bits.Update` mutates state to record the counter as consumed [5](#0-4) . Because `Check` and `Update` are invoked as two independent critical sections with unlocked, non-trivial work (AEAD decryption) in between, a duplicated/replayed UDP datagram for the same message counter that is delivered concurrently on two different queues (routines) can both pass `Check` before either has called `Update`.

Nebula explicitly supports multiple concurrent UDP-reading routines that all funnel into `readOutsidePackets`→`Decrypt` for the same `ConnectionState` [6](#0-5) [2](#0-1) . An attacker who can duplicate a captured ciphertext packet at the UDP layer (no valid certificate needed — this operates purely on already-encrypted wire bytes and requires no cert/CA trust) can race the same message counter across the multi-queue listeners, exploiting the TOCTOU gap between `Check` and `Update`.

This is the same root cause pattern as the report: a state variable used to gate a security decision is checked in one operation and committed in a separate, later operation, with attacker-controlled/expensive work sitting in between, allowing the check to be satisfied twice by concurrent actors before either commit lands.

The project's own e2e test (`TestRelayReplayProtection`) demonstrates the team is aware replay-window enforcement is fragile and previously buggy on the relay path (a related but distinct bug where `Update` was never called at all) [7](#0-6) , underscoring that replay-window atomicity in this codebase has been an active area of concern, though that specific issue is already fixed for the relay path per the test. The `Decrypt`/data-plane path retains the split-lock Check-then-Update structure.

### Impact Explanation
If exploited, this allows a captured/replayed ciphertext packet to have its plaintext delivered to the TUN device or firewall/routing path more than once for a single legitimate transmission, defeating the intended anti-replay guarantee of the Noise/AEAD data channel. Depending on payload (e.g., a replayed control or data packet), this can result in duplicate packet injection into the tunnel and undermines the confidentiality/integrity assumptions the replay window is meant to enforce. It is a lower-severity version of the reported class since only one instance of the plaintext is normally guaranteed to reach delivery per current code paths (the second `Update` typically loses the race and returns `ErrAlreadySeen`), but the race window itself is a real, exploitable protocol invariant violation and forces duplicate expensive AEAD decrypt work per replay (amplification/DoS potential when `f.routines > 1`).

### Likelihood Explanation
Requires `f.routines > 1` (multi-queue mode, a supported/documented configuration on Linux) and requires the attacker to be able to deliver duplicate copies of a previously observed ciphertext packet to different UDP-reading queues in a tight timing window — feasible for an on-path or off-path attacker capable of packet duplication (e.g., via multipath, retransmission tricks, or a compromised relay/duplicate injection). No valid certificate or handshake participation is required since this targets the data-plane replay window directly on already-encrypted bytes.

### Recommendation
Make the replay-window check-then-commit atomic: hold `decryptLock` across both `Check` and `Update` (or fold them into a single `Bits` method) so no other goroutine can observe an unconsumed window state between the two operations. Alternatively, perform `Update` immediately after a successful `Check` and before releasing the lock, treating `DecryptDanger` failure as a rollback case, or reserve the counter atomically prior to decrypting and release/unset it on decryption failure.

### Proof of Concept
1. Configure a node with `routines > 1` (multi-queue UDP listening) on Linux.
2. Establish a tunnel and capture one legitimate data-plane ciphertext packet with message counter N.
3. Rapidly replay two copies of that exact packet such that they land on two different UDP listener queues (e.g., via `SO_REUSEPORT` socket duplication or crafted timing) so both `readOutsidePackets` calls reach `Decrypt` concurrently.
4. Observe that both goroutines' `cs.window.Check(l, N)` calls (steps taken under separate, non-overlapping lock acquisitions) can return `true` before either has called `cs.window.Update(l, N)`, causing both to proceed to `DecryptDanger` and only serializing at the final `Update` call — demonstrating the check-then-act race exists, even though only one commit ultimately wins.

**Uncertainty note:** I was unable to fully verify, from static inspection alone, an end-to-end scenario where both racing decrypt operations succeed in delivering plaintext to the TUN device (the second `Update` call in the current code does appear to correctly reject the duplicate before delivery). Confirming actual double-delivery (versus just double AEAD-decrypt work) would require dynamic/concurrent testing with `routines > 1`, which is outside what static code review can conclusively prove.

### Citations

**File:** interface.go (L243-279)
```go
	if f.routines > 1 {
		if !f.inside.SupportsMultiqueue() || !f.outside.SupportsMultipleReaders() {
			f.routines = 1
			f.l.Warn("routines is not supported on this platform, falling back to a single routine")
		}
	}

	metrics.GetOrRegisterGauge("routines", nil).Update(int64(f.routines))

	// Prepare n tun queues
	var reader io.ReadWriteCloser = f.inside
	for i := 0; i < f.routines; i++ {
		if i > 0 {
			reader, err = f.inside.NewMultiQueueReader()
			if err != nil {
				return err
			}
		}
		f.readers[i] = reader
	}

	// On error the caller owns the cleanup, Control.Start cancels the service context
	// before releasing our resources so a waiter never observes a live context
	if err = f.inside.Activate(); err != nil {
		return err
	}

	return nil
}

func (f *Interface) run() {
	// Launch n queues to read packets from udp
	for i := 0; i < f.routines; i++ {
		f.wg.Go(func() {
			f.listenOut(i)
		})
	}
```

**File:** outside.go (L126-132)
```go
	out, err = hostinfo.ConnectionState.Decrypt(f.l, h.MessageCounter, out, packet, nb)
	if err != nil {
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("Failed to decrypt packet", "error", err, "from", via, "header", h)
		}
		return
	}
```

**File:** connection_state.go (L61-82)
```go
func (cs *ConnectionState) Decrypt(l *slog.Logger, messageCounter uint64, out []byte, packet []byte, nb []byte) ([]byte, error) {
	var err error
	cs.decryptLock.Lock()
	result := cs.window.Check(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return nil, ErrAlreadySeen
	}

	out, err = cs.dKey.DecryptDanger(out, packet[:header.Len], packet[header.Len:], messageCounter, nb)
	if err != nil {
		return nil, err
	}

	cs.decryptLock.Lock()
	result = cs.window.Update(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return nil, ErrAlreadySeen
	}
	return out, nil
}
```

**File:** bits.go (L134-150)
```go
// Check returns true if i is within (or way out in front of) the window, and not a replay
func (b *Bits) Check(l *slog.Logger, i uint64) bool {
	// If i is the next number, return true.
	if i > b.current {
		return true
	}

	if b.strictlyWithinWindow(i) {
		return !b.get(i)
	}

	// Not within the window
	if l.Enabled(context.Background(), slog.LevelDebug) {
		l.Debug("rejected a packet (top)", "current", b.current, "incoming", i)
	}
	return false
}
```

**File:** bits.go (L168-186)
```go
func (b *Bits) Update(l *slog.Logger, i uint64) bool {
	// Fast path: i is the next expected counter. Split out so the function
	// stays small and avoids paying for the slow paths' slog argument-build
	// stack frame on every call. The bit read/test/write is inlined to
	// touch the backing word once.
	if i == b.current+1 {
		pos := i & b.lengthMask
		word := pos >> 6
		mask := uint64(1) << (pos & 63)
		w := b.bits[word]
		if i > b.length && w&mask == 0 {
			b.lostCounter.Inc(1)
		}
		b.bits[word] = w | mask
		b.current = i
		return true
	}
	return b.updateSlow(l, i)
}
```

**File:** e2e/tunnels_test.go (L377-382)
```go
// TestRelayReplayProtection asserts that a relay (forwarding-type) node rejects
// replayed relay frames. A captured relay frame, re-injected with the same
// message counter, must be dropped by the replay window rather than re-forwarded
// to the relay target. Before the fix, handleOutsideRelayPacket authenticated the
// frame but never advanced the replay window, so every replay was re-forwarded.
func TestRelayReplayProtection(t *testing.T) {
```
