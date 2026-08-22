### Title
Check-then-Act race in replay-window validation allows a captured packet to be decrypted twice before the duplicate is rejected - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` validate an incoming message counter against the anti-replay window in two separate, non-atomic critical sections: `window.Check()` is called and its lock released, the AEAD decryption/verification runs *unlocked*, and only afterward is `window.Update()` called under a fresh lock to actually record the counter as seen. This is the same "front-run" bug class as the reported `close()`/`accrueInterest()` issue: a caller's decision (based on a state read) is invalidated by a second, concurrently front-running caller before the first caller's later write commits, letting an event proceed on stale state.

### Finding Description
`Decrypt` in [1](#0-0)  performs:
1. `decryptLock.Lock(); window.Check(counter); decryptLock.Unlock()`
2. AEAD `DecryptDanger` (unlocked, can take non-trivial time)
3. `decryptLock.Lock(); window.Update(counter); decryptLock.Unlock()`

`window.Check` only tests whether `counter` has already been marked seen; it does not mark it seen itself, and marking only happens in `Update`, a separate call [2](#0-1) . Between step 1 and step 3, the same counter is not yet recorded as consumed, so any other goroutine processing the exact same packet (or an attacker-replayed copy of it) concurrently will also pass `Check` and also successfully run `DecryptDanger`, producing a second valid plaintext before `Update` for either call has run. `VerifyRelay` has the identical Check→verify→Update pattern for the relay path [3](#0-2) .

The UDP read path in `readOutsidePackets` runs on multiple listener/worker goroutines (`q` is a per-queue/worker index) and feeds every inbound packet straight into `hostinfo.ConnectionState.Decrypt` [4](#0-3)  without any per-hostinfo serialization prior to `Decrypt`. Nothing prevents two workers from concurrently handing the same wire packet (an attacker's captured/replayed copy, delivered twice via UDP duplication or by an active on-path attacker re-injecting it) to `Decrypt` for the same `ConnectionState`.

This mirrors the report's root cause exactly: a two-step "read state, act, then commit state" sequence where the commit is delayed relative to the read, letting a second actor "front-run" the commit and get treated as if the first action hadn't happened yet.

### Impact Explanation
If the same on-wire message counter can be decrypted more than once, the anti-replay guarantee of the tunnel is broken: a passively captured (or actively duplicated) encrypted packet can be reprocessed by the data path a second time, and downstream consumers (`handleOutsideMessagePacket`, `LightHouse.HandleRequest`, `handleOutsideRelayPacket`) may act on it twice. For relay frames this is explicitly the failure mode the codebase has previously fixed for a related but distinct bug: "Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them" (#1751) and the dedicated regression test `TestRelayReplayProtection` [5](#0-4) , showing the project treats duplicate re-processing of AEAD-authenticated frames as a security-relevant replay bypass, not a cosmetic bug.

### Likelihood Explanation
Exploitation requires only the ability to deliver the identical UDP packet twice in a very tight window relative to the target's own worker scheduling (e.g., a passive/off-path attacker who captured a legitimate frame and races it in twice, or a local network condition causing duplicate delivery). No CA-signed certificate is needed - the attacker only needs a copy of ciphertext previously observed on the wire; the vulnerability is purely in how the replay window's check and commit are split, independent of any peer authentication. The window for the race is nondeterministic (bounded by AEAD decrypt time) so likelihood is moderate rather than trivially reliable, but repeated attempts materially increase the chance of a hit.

### Recommendation
Make the replay check-and-mark atomic with respect to decryption ordering: hold `decryptLock` (or use a per-counter CAS/claim) across both the `Check` and the point where the counter is irrevocably committed as seen, or restructure so `Update` is attempted first (claiming the slot) and rolled back only if the subsequent AEAD verification fails, ensuring no two concurrent callers can ever observe "not yet seen" for the same counter and both proceed to decrypt.

### Proof of Concept
Conceptual PoC (not executed):
1. Establish a tunnel and capture one legitimate data-plane frame `P` with message counter `N`.
2. From an attacker-controlled vantage point capable of packet duplication (e.g., a local network tap, or by re-injecting `P` twice back-to-back over UDP), deliver two copies of `P` to the victim's listener socket in immediate succession so they land on two different reader/worker goroutines.
3. Both goroutines call `hostinfo.ConnectionState.Decrypt(l, N, ...)` for the same `ConnectionState`. Because `Check` and `Update` are separate critical sections with unlocked AEAD work in between, both goroutines can pass `Check(N)` before either calls `Update(N)`, so both successfully decrypt and both proceed to `handleOutsideMessagePacket`/`LightHouse.HandleRequest`/etc.
4. Only the second `Update(N)` call will report a duplicate, but by then the packet has already been decrypted and dispatched twice, i.e. the replay was not actually blocked from being processed.

Note: I was not able to run this scenario in an actual multi-goroutine timing test within this session, so the exact race window size and real-world reliability of triggering both goroutines to interleave between `Check` and `Update` remains unverified and should be confirmed with a targeted concurrency test (similar to `TestRelayReplayProtection`) before treating this as fully proven in practice.

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

**File:** connection_state.go (L85-107)
```go
func (cs *ConnectionState) VerifyRelay(l *slog.Logger, messageCounter uint64, packet []byte, nb []byte) error {
	cs.decryptLock.Lock()
	result := cs.window.Check(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return ErrAlreadySeen
	}

	signedPayload := packet[:len(packet)-cs.dKey.Overhead()]
	signatureValue := packet[len(packet)-cs.dKey.Overhead():]
	_, err := cs.dKey.DecryptDanger(nil, signedPayload, signatureValue, messageCounter, nb)
	if err != nil {
		return err
	}

	cs.decryptLock.Lock()
	result = cs.window.Update(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return ErrAlreadySeen
	}

	return nil
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

**File:** e2e/tunnels_test.go (L377-382)
```go
// TestRelayReplayProtection asserts that a relay (forwarding-type) node rejects
// replayed relay frames. A captured relay frame, re-injected with the same
// message counter, must be dropped by the replay window rather than re-forwarded
// to the relay target. Before the fix, handleOutsideRelayPacket authenticated the
// frame but never advanced the replay window, so every replay was re-forwarded.
func TestRelayReplayProtection(t *testing.T) {
```
