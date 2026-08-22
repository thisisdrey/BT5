### Title
Replay-window check-then-act race allows double processing of a single replayed/duplicate packet - ([File: connection_state.go])

### Summary
The reported PoolVoter bug is a classic check-then-act flaw: `distributeEx()` reads `periodFinish[token]`/`rewardRate[token]`, and a second call in the same block sees stale state before the first call's effects are fully "settled", letting the caller double-count a distribution. The closest reachable analog in Nebula's data plane is the anti-replay window in `ConnectionState.Decrypt`/`VerifyRelay`, which also splits a "check" step and an "act" (commit) step across two separately-locked critical sections, with the expensive decrypt operation happening in between, unlocked.

### Finding Description
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` in `connection_state.go` implement anti-replay via `Bits.Check()` followed later by `Bits.Update()`, but they are not atomic together: [1](#0-0) 

The sequence is: (1) lock, call `window.Check(l, messageCounter)`, unlock; (2) perform AEAD decryption outside any lock; (3) lock again, call `window.Update(l, messageCounter)`, unlock. `Check` is read-only — it does not mark the counter as seen — so if the same message counter is delivered twice to the same `ConnectionState` before the first delivery reaches step 3, both invocations can pass `Check` and both proceed to decrypt and, in `Decrypt`'s caller, hand a duplicate application packet to the TUN device. Only the later `Update` call would return `false` for the second copy, but by then the (already duplicated) plaintext packet has already been decrypted and, depending on caller ordering, potentially already injected to the tun device before the duplicate is detected.

This mirrors the reported bug's root cause: relying on a stale read (`periodFinish`/`rewardRate`, here `window` bitmap state) that hasn't yet been committed by a concurrent or repeated call, allowing the same unit of "reward"/"data" to be processed more than once.

The CHANGELOG for this repo shows the project has already fixed adjacent replay issues — "Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them" and "Lock replay window updates so concurrent readers can't corrupt it" — confirming replay-window handling has been an active area of hardening: [2](#0-1) 

However, those fixes address forwarding/corruption, not the Check/Update split itself, which remains structurally a two-step, non-atomic operation: [3](#0-2) 

### Impact Explanation
If an attacker with network position can duplicate or race two copies of the same encrypted packet to a node (a capability nebula's replay window is explicitly designed to prevent), a race between the `Check` and `Update` critical sections could result in a single ciphertext being decrypted and delivered twice, i.e. a partial replay-protection bypass. This falls under "traffic decryption/forgery/replay" impact category. It does not require a CA-signed certificate to exploit — it only requires the ability to duplicate a UDP packet in flight, e.g., by IP fragmentation duplication, a malicious router, or network-level retransmission games.

### Likelihood Explanation
Exploitability is uncertain and I could not fully verify it: the `decryptLock`/window locks serialize `Check` and `Update` on their own, but the AEAD `DecryptDanger` call between them is unlocked, so two goroutines both calling `Decrypt` concurrently for the same `hostinfo`/`ConnectionState` could each pass `Check` before either calls `Update`. Whether the interface actually dispatches concurrent reads for the same `hostinfo` (multiple reader routines processing packets from the same UDP conn/index concurrently) was not confirmed within the available index/tool budget — I found the packet-reading path (`readOutsidePackets` in `outside.go`) but could not confirm the `routines` configuration and goroutine fan-out model from `main.go`/`interface.go` before running out of tool calls. This limits confidence that the race is practically triggerable versus being naturally serialized by a single-reader-per-connection design.

### Recommendation
Not applicable without confirming exploitability — the analog is speculative pending goroutine/dispatch-model confirmation. If confirmed reachable, `Check`+`Update` should be combined into a single atomic "check-and-mark" operation under one lock acquisition (rather than two separate lock/unlock cycles wrapping the decrypt step), so no two concurrent deliveries of the same counter can both pass the check before either is marked seen.

### Proof of Concept
Not able to construct a verified PoC; this requires confirming (1) that two goroutines can concurrently invoke `ConnectionState.Decrypt` for the same `hostinfo`/index, and (2) that a real UDP-level duplicate of a captured packet reaches `readOutsidePackets` twice in a sufficiently tight window to race the two `Check` calls before either `Update` commits. I could not verify condition (1) within the remaining investigation budget.

### Citations

**File:** connection_state.go (L61-81)
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
```

**File:** CHANGELOG.md (L79-82)
```markdown
- Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them. (#1751)
- Fix a race in relay state handling. (#1753)
- Lock replay window updates so concurrent readers can't corrupt it. (#1802)
- Reject malformed handshakes more reliably, including invalid ed25519 key lengths. (#1601, #1756)
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
