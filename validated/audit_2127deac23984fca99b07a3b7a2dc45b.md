### Title
Replay-window check/update race allows duplicate packet replay bypass - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` split the anti-replay window verification into two separately-locked critical sections: `window.Check()` is called and the lock released, decryption happens *unlocked*, then the lock is re-acquired to call `window.Update()`. This mirrors the reported analog bug class: a security-relevant decision (accepting a message counter as "not yet seen") is made against a state snapshot that does not account for a "pending" mutation of that same state which is still in flight, permitting the value to be used twice before it is finally recorded.

### Finding Description
`Bits.Check()` only tests whether a counter has already been marked seen; `Bits.Update()` is what actually marks it seen. In the data-plane decrypt path these two operations are not performed atomically with respect to each other: [1](#0-0) 

```
cs.decryptLock.Lock()
result := cs.window.Check(l, messageCounter)
cs.decryptLock.Unlock()
...
out, err = cs.dKey.DecryptDanger(...)
...
cs.decryptLock.Lock()
result = cs.window.Update(l, messageCounter)
cs.decryptLock.Unlock()
```

Between the `Check` call and the `Update` call the lock is fully released. If two packets carrying the same `messageCounter` are processed concurrently by two goroutines (e.g. via multiple UDP reader routines processing packets for the same tunnel, or an attacker duplicating an intercepted ciphertext packet so it is delivered twice in quick succession), both goroutines can observe `Check() == true` (not yet seen) before either has called `Update()` to mark the counter as consumed. Both then proceed to decrypt and accept the packet as valid, and only the “Update” bookkeeping is serialized after the fact — the decrypted, accepted result has already been used.

The identical pattern exists in `VerifyRelay`, used to authenticate AEAD-protected relay frames forwarded through relay nodes: [2](#0-1) 

This is structurally the same root cause as the reported Omnipool bug: a piece of state (`totalSupply`, here the replay bitmap) is read via a “Check”-like snapshot that ignores a “pending” concurrent mutation of that same state, so the security decision made from the stale snapshot is wrong once the pending mutation actually lands.

### Impact Explanation
An attacker who can capture and re-inject a single legitimate ciphertext packet (a passive/on-path network attacker, requiring no certificate or prior authentication) may be able to get that packet accepted twice by racing the check/update window, defeating Nebula's anti-replay protection for that message. Since Nebula's replay window is the sole mechanism preventing acceptance of duplicate/replayed ciphertext (see `TestRelayReplayProtection`, which explicitly documents that a missing replay-window update previously allowed indefinite re-forwarding of a captured relay frame), a successful race reintroduces a traffic-replay primitive against tunnel traffic or relay frames. [3](#0-2) 

### Likelihood Explanation
This requires winning a narrow race window (the time between releasing the lock after `Check` and re-acquiring it after decryption) and requires the attacker to be able to deliver the exact same ciphertext twice in a way that lands on two concurrently-executing decrypt calls for the same `ConnectionState`. This is plausible on relay paths and multi-routine UDP configurations but is timing-dependent, so likelihood is moderate rather than trivially reliable.

### Recommendation
Hold `decryptLock` for the entire duration of `Check` → `Decrypt` → `Update` (or otherwise make the check-then-mark operation atomic, e.g. by combining `Check`+`Update` into a single locked "reserve" step performed before decryption, and rolling back only on decryption failure) in both `ConnectionState.Decrypt` and `ConnectionState.VerifyRelay`.

### Proof of Concept
Not independently reproduced (requires a timing-sensitive concurrent harness); the race is demonstrated structurally by the code excerpts above: `window.Check()` returns `true` (not yet seen) while `decryptLock` is unlocked and `window.Update()` for the same counter has not yet executed, so two concurrent calls for the same `messageCounter` can both pass `Check` before either calls `Update`.

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

**File:** e2e/tunnels_test.go (L377-382)
```go
// TestRelayReplayProtection asserts that a relay (forwarding-type) node rejects
// replayed relay frames. A captured relay frame, re-injected with the same
// message counter, must be dropped by the replay window rather than re-forwarded
// to the relay target. Before the fix, handleOutsideRelayPacket authenticated the
// frame but never advanced the replay window, so every replay was re-forwarded.
func TestRelayReplayProtection(t *testing.T) {
```
