## Title
Replay-window check/decrypt/update is not atomic, allowing a captured ciphertext to be replayed and re-accepted under concurrent packet processing - (File: connection_state.go)

## Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` in `connection_state.go` split the anti-replay decision into three separate, individually-locked steps: `window.Check()` (read-only test), AEAD decryption (no lock), and `window.Update()` (the actual commit that marks the counter as seen). The lock is released between `Check` and the decrypt call, and re-acquired only around `Update`. This is structurally the same bug class as the Ajna `kick()`/LUP issue: a decision value is computed against the pre-mutation state, work proceeds based on that stale decision, and the state is only "finalized" afterward — so if a second copy of the same packet is processed concurrently, both copies can observe the same not-yet-updated window state and both get treated as valid.

## Finding Description
`Decrypt` (and analogously `VerifyRelay`) is invoked from the outside-packet path (`outside.go` → `f.readOutsidePackets` → `hostinfo.ConnectionState.Decrypt(...)` / `VerifyRelay(...)`), which processes attacker-controlled UDP datagrams that arrive on the wire. An attacker who captures one legitimate ciphertext packet does not need a CA-signed certificate or a valid key to replay that exact packet — they simply resend the captured bytes to the same UDP listener.

The replay-window logic (`bits.go`) is a `Check`-then-`Update` state machine: `Check` only inspects the window state, `Update` is the operation that actually marks a given counter as seen and can concurrently mutate `Bits.current`/`bits`. In `connection_state.go`: [1](#0-0) 

The sequence is:
1. Lock, `window.Check(messageCounter)`, unlock.
2. Decrypt (no lock held).
3. Lock, `window.Update(messageCounter)`, unlock.

Because the lock is dropped between steps 1 and 3, this is not an atomic check-and-set. If two goroutines race with the identical `messageCounter` (e.g., the same captured packet delivered twice — over multiple UDP read routines, via a relay path, or simply by an attacker flooding duplicates before the first copy's `Update` call completes), both can pass `Check` before either calls `Update`, since neither has yet mutated the window. Both then proceed to decrypt (which will succeed identically for identical ciphertext) and both attempt `Update`; only one will "win" the second lock acquisition and register the bit, but both already completed decryption and (depending on caller) may have already been handed off to be written to `tun` or forwarded (relay case) by the time the second `Update` returns false.

This mirrors the reported bug class exactly: `_kick()` computes LUP against `poolState.debt`, then mutates debt (adds kick penalty) afterward, and the already-computed LUP is used/returned without being recomputed against the final state — a decision is made from stale state and then committed as if it accounted for the mutation. Here, `Check` is the "decision" made against not-yet-updated window state, and `Update` is the deferred "final" state commit, exactly the same anti-pattern of separating a read-decision from the write-commit that should be atomic.

## Impact Explanation
If exploitable in a build/config where outside-packet handling runs with concurrency (multiple UDP listener goroutines / multiple routines feeding the same `HostInfo`), this allows a captured/duplicated ciphertext to be decrypted and delivered to the tun device (or re-forwarded by a relay, cf. the `TestRelayReplayProtection` test's own callout that a prior bug allowed "every replay was re-forwarded") more than once, defeating Nebula's AEAD/Noise anti-replay guarantee. Repeated replay could be used to re-inject stale application-layer data or to duplicate relay traffic, which is a concrete violation of "replay handling" that the scan explicitly calls in-scope (traffic decryption/forgery/replay).

## Likelihood Explanation
Exploitability depends on whether the affected `HostInfo`/`ConnectionState` can actually have `Decrypt`/`VerifyRelay` invoked concurrently for the same tunnel (e.g., `tun.routines`/multiple listener routines feeding `readOutsidePackets` for the same hostinfo, or a relay forwarding duplicate frames near-simultaneously). This is plausible in Nebula's architecture (multiple UDP read routines are a documented feature), but I could not fully confirm from the indexed code whether per-hostinfo processing is otherwise serialized elsewhere (e.g., by NIC queue affinity or a lock I did not locate) that would prevent the race in practice. This uncertainty should be verified against the full source before treating this as a confirmed, easily-triggerable bug rather than a narrow race window.

## Recommendation
Make the replay check-and-set atomic: hold `decryptLock` (or a per-counter test-and-set) across `Check` through `Update`, or fold `Check`+`Update` into a single atomic `CheckAndUpdate` call performed once, with decryption only proceeding after the bit is provisionally marked (and rolled back on decrypt failure). This removes the window between "decision" and "commit" instead of allowing the stale, pre-mutation check result to authorize completion of the operation, directly analogous to the fix recommended for `_kick()` (compute the final decision only after all state mutations are applied, as the last step).

## Proof of Concept
1. Establish a tunnel between two Nebula nodes so ConnectionState/Decrypt is active.
2. Capture one legitimate ciphertext frame in flight (as already demonstrated feasible by the existing `TestRelayReplayProtection` harness in `e2e/tunnels_test.go`, which captures a `relayFrame` and re-injects it).
3. Instead of re-injecting sequentially, re-inject the identical captured frame twice "simultaneously" (e.g., from two goroutines) targeting the same `HostInfo`/`ConnectionState`, timed to land while a `Decrypt`/`Update` cycle for the first copy is between the `Check` and `Update` locked sections.
4. If the runtime schedules two `readOutsidePackets` calls concurrently for the same `ConnectionState` counter before the first `Update` commits, both `Check` calls return `true`, both packets are decrypted and (in the relay case) both are forwarded — observable as more than one forwarded/delivered copy of the same counter, which should never happen once replay protection is engaged. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

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

**File:** bits.go (L134-186)
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

// Update has three branches:
//   - i == b.current+1: fast path; advance the cursor by one and lose-count
//     the slot we just stomped (only past warmup; see the i > b.length guard
//     below).
//   - i  >  b.current+1: jump path; clear all slots between current and i
//     (or up to a full window's worth, whichever is smaller) via clearRange,
//     then mark i. Two arms here: a warmup arm that handles the very first
//     window before the cursor has slid, and a steady-state arm that treats
//     every cleared empty slot as a lost packet.
//   - i  <= b.current: in-window check for duplicates; out-of-window otherwise.
//
// NewBits seeds bits[0]=1 so counter 0 looks "received" — Update never
// clears that marker during warmup (clearRange skips position 0 when
// startPos=1), and once b.current >= b.length the marker is no longer
// consulted. The marker prevents a fictitious "lost" hit on the first real
// counter.
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

**File:** outside.go (L113-120)
```go
	// All remaining packets are encrypted
	if isMessageRelay {
		// Relay packets are special, this branch should always early-return
		if err = hostinfo.ConnectionState.VerifyRelay(f.l, h.MessageCounter, packet, nb); err != nil {
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				hostinfo.logger(f.l).Debug("Failed to verify relay packet", "error", err, "from", via, "header", h)
			}
			return
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
