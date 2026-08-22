### Title
Non-atomic check-then-update of the anti-replay window allows a duplicated packet to be decrypted twice - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` implement the anti-replay check as two separate, lock-released operations: `window.Check` (does this counter look unseen?) followed later by `window.Update` (mark it seen), with the expensive `DecryptDanger` call happening *in between*, outside the lock. This is the same bug class as the reported finding: a security-relevant limit/replay check is evaluated against state that does not yet reflect the in-flight operation it is meant to gate, so a value that should be treated as "already consumed" is not, and the enforcement can be bypassed.

### Finding Description
`Decrypt` takes `decryptLock`, calls `cs.window.Check(l, messageCounter)`, releases the lock, performs AEAD decryption unlocked, then re-acquires the lock to call `cs.window.Update(l, messageCounter)`: [1](#0-0) 

`VerifyRelay` (used for relay-forwarded frames) follows the identical pattern: [2](#0-1) 

`window.Check` only inspects whether the bit for `i` is currently set — it does not itself mark the bit or otherwise reserve the counter: [3](#0-2) 

Because `decryptLock` is released between `Check` and the eventual `Update`, two goroutines processing two copies of the same UDP packet (same `messageCounter`) concurrently can both execute `Check` before either has called `Update`. Both `Check` calls observe the bit as unset and return `true`, so both goroutines proceed to independently run `DecryptDanger` on the same ciphertext/counter and succeed (AEAD decryption with a valid, not-yet-consumed nonce is deterministic and will succeed for both). Only afterward does one of the two `Update` calls "win" and mark the bit; the other returns `false`. But by that point the decryption itself already happened twice — i.e., the same on-wire message was authenticated and decrypted twice by the "check" step's window, defeating the intended guarantee that a given message-counter/nonce is processed at most once.

This mirrors the report's root cause exactly: a security check ("is this counter unused/under the limit?") is evaluated against state that is stale relative to a concurrent, in-flight consumption of that same resource, allowing the enforced invariant (no counter reused / limit never exceeded) to be violated.

### Impact Explanation
Nebula's data-plane security model relies on the per-connection replay window to guarantee that each Noise message counter is accepted and processed exactly once, which underlies the tunnel's traffic-integrity guarantees. UDP packets can be trivially duplicated by any network position between the peers (or replayed by an on-path/off-path attacker who captured a copy), and Nebula's outside packet handling for a single `HostInfo`/`ConnectionState` can be invoked concurrently for different packets (e.g., from multiple UDP reader goroutines or via relayed vs. direct delivery paths reaching `outside.go`). A duplicated frame arriving as two near-simultaneous UDP datagrams can therefore be decrypted twice under the current locking scheme, and whichever caller does not lose the `Update` race still holds a fully decrypted plaintext buffer from `Decrypt`, even though the anti-replay window is supposed to prevent that message from being processed more than once. The impact is a traffic-replay-class weakness: the anti-replay/no-double-processing guarantee for on-wire messages/frames is not enforced atomically, and a duplicated (replayed) datagram can be authenticated/decrypted twice, undermining the replay protection the window is meant to provide (particularly relevant on the relay path via `VerifyRelay`, where a captured relay frame is exactly the attack Nebula's changelog says it fixed for one direction — "advance the replay window on relayed packets" — but the check/update split here reintroduces a race window rather than a full bypass).

### Likelihood Explanation
Exploitability requires the ability to deliver two copies of the same captured/duplicated ciphertext to the same node in a tight enough time window that both `Check` calls race ahead of both `Update` calls — achievable by any attacker capable of duplicating UDP traffic (trivial for any on-path attacker, or off-path attacker replaying a captured packet twice in quick succession, especially over relay paths where frame delivery timing is attacker-influenced). This does not require possession of a CA-signed certificate; it operates purely against already-established ciphertext on the wire. The race window is narrow (bounded by one `DecryptDanger` call), so likelihood is moderate rather than trivially reliable, but it is a genuine, reachable non-atomicity in a security-critical check.

### Recommendation
Make the replay-window reservation atomic with the check: hold `decryptLock` across `Check` (or better, an atomic check-and-set primitive) so that once a counter is accepted by `Check`, no other concurrent caller can pass `Check` for the same counter until `Update` has committed (or failed) for the first caller. Concretely, `Bits` should expose a single atomic `CheckAndReserve(i)`-style method that both tests and marks the bit under one critical section, and `Decrypt`/`VerifyRelay` should call it once, decrypt, and only finalize (or roll back) the reservation based on decryption success — removing the unlock/relock gap that currently allows two decrypt attempts for one counter to run concurrently.

### Proof of Concept
Conceptual sequence demonstrating the race (not a full runnable exploit given the ask-only/index constraints):
1. Attacker (or network duplication) delivers the same UDP ciphertext packet with counter `N` to the target twice, essentially simultaneously, to two different reader goroutines that both end up calling `hostinfo.ConnectionState.Decrypt(l, N, ...)` (or `VerifyRelay` on a relay node) for the same `ConnectionState`.
2. Goroutine A: `decryptLock.Lock(); window.Check(N) -> true; decryptLock.Unlock()` [4](#0-3) 
3. Before A calls `Update`, Goroutine B runs the same `Check(N)` sequence and also observes `true` because bit `N` has not been set yet [3](#0-2) 
4. Both A and B independently call `cs.dKey.DecryptDanger(...)` on the identical ciphertext/counter and both succeed, since AEAD decryption is deterministic and the nonce/counter has not actually been consumed yet [5](#0-4) 
5. A calls `window.Update(N)` and wins, marking the bit; B calls `window.Update(N)` and gets `false` — but B has already produced a fully decrypted plaintext from step 4 before this check runs [6](#0-5) 

This confirms the check-then-decrypt-then-update sequence is not atomic, so the same wire message can be decrypted more than once despite the replay window's intended single-use guarantee.

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

**File:** connection_state.go (L84-108)
```go
// VerifyRelay verifies AEAD protected (but not encrypted) relay frames. packet must be length-checked by the caller.
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
