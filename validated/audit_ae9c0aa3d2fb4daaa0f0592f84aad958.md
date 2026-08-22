### Title
Check-then-Act Race in Replay-Window Validation Allows Duplicate Packet Delivery - (File: `connection_state.go`)

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` validate the anti-replay window by calling `Bits.Check()` and `Bits.Update()` as two separate, independently-locked operations, with the AEAD decrypt/verify sandwiched in between and the mutex released after `Check` and re-acquired before `Update`. This is the same "check-then-act with an unguarded gap" pattern as the reported `depositERC20` reentrancy bug: the guard that should make validate-then-commit atomic is split into two critical sections, leaving a window in which the same message counter can pass validation twice before the window state is actually updated to reflect it as seen.

### Finding Description
`Decrypt` performs:
1. `decryptLock.Lock(); result := window.Check(l, messageCounter); decryptLock.Unlock()` — checks if `messageCounter` is fresh/replay, but does **not** mark it as seen.
2. `dKey.DecryptDanger(...)` — AEAD decrypt, unlocked, no dependency on window state.
3. `decryptLock.Lock(); result = window.Update(l, messageCounter); decryptLock.Unlock()` — actually marks the counter as consumed. [1](#0-0) 

`VerifyRelay` follows the identical pattern for relay-forwarded frames. [2](#0-1) 

Because the lock is dropped between `Check` and `Update`, if the packet pipeline invokes `Decrypt`/`VerifyRelay` concurrently for the same `ConnectionState` (e.g., two UDP reader/worker goroutines processing a duplicated on-wire packet, or an attacker sending the same encrypted message counter twice in quick succession so both copies are picked up by different reader routines before the first one finishes), both goroutines can pass `Check` (since neither has yet called `Update`), both successfully decrypt (AEAD decryption is deterministic and doesn't consult window state), and only afterward does one of the two `Update` calls fail while the other succeeds. By the time the failing `Update` is discovered, the duplicate plaintext has already been produced and — in the caller path — is delivered onward (e.g., written to the TUN device or forwarded, in the relay case) before the replay is detected. This mirrors the reported bug class exactly: state mutation intended to gate a side effect (crediting a deposit balance / marking a counter "seen") is deferred past the point where the side effect (token transfer / packet delivery) already occurred, and the reentrant/concurrent second call slips through the gap.

### Impact Explanation
This allows a remote, unauthenticated-by-certificate attacker (any peer capable of sending replayed UDP frames toward a connection, including a MITM on the underlay network) to cause duplicate processing of a single legitimate encrypted message: duplicate delivery to the TUN interface, or in the relay path, duplicate re-forwarding of a relay frame despite replay protection intending to prevent exactly that (see the existing regression test guarding relay replay behavior). This is a concrete instance of "remote state poisoning" / anti-replay bypass — the very defense mechanism (`Bits` sliding window) can be defeated by racing the check against the update, defeating its purpose of preventing duplicate/replayed traffic from being processed twice.

### Likelihood Explanation
Exploitability depends on the packet-processing pipeline dispatching concurrent workers that can invoke `Decrypt`/`VerifyRelay` on the same `ConnectionState` for near-simultaneous copies of the same wire packet (e.g., duplicated by the underlying network, or intentionally sent twice by an on-path attacker with tight timing). The `decryptLock` mutex is `ConnectionState`-scoped, so the race window exists only for the same tunnel, but nothing in `Decrypt`/`VerifyRelay` prevents two calls with the identical `messageCounter` from both passing `Check` before either reaches `Update`. Given nebula's UDP receive path is designed for concurrency (multiple worker routines processing inbound packets), this is a realistic condition, though it requires precise timing to win the race reliably.

### Recommendation
Collapse `Check` and `Update` into a single atomic operation performed under one lock acquisition that spans decrypt-then-commit, or perform the "reserve" step before decrypting and only "commit" (finalize) after decrypt succeeds, all under a single critical section per messageCounter — i.e., do not release `decryptLock` between validating freshness and marking the counter consumed. Concretely, add a `Bits.CheckAndReserve` (analogous to `Update`, but callable before the AEAD result is known, with a corresponding rollback path if decrypt fails) so the entire "check-decrypt-commit" sequence for a given counter is effectively single-threaded per `ConnectionState`, closing the gap where two concurrent calls can both observe `Check == true`.

### Proof of Concept
1. Establish a nebula tunnel between two nodes.
2. Capture a legitimate data-plane packet (or relay frame) with counter `N`.
3. Re-inject two copies of the same captured frame at effectively the same time (e.g., from two goroutines calling into the UDP receive path, or via two near-simultaneous socket writes) so both are picked up by concurrent reader workers before either completes `Decrypt`.
4. Observe that both calls pass `window.Check(l, N)` (returning `true`) because neither has yet reached `window.Update(l, N)`; both successfully `DecryptDanger` the same ciphertext; the duplicate plaintext is delivered/forwarded twice, and only the second `Update` call subsequently reports `ErrAlreadySeen` — after the damage (duplicate delivery) is already done. This is directly analogous to the `depositERC20` scenario, where the balance-crediting/side-effect step was allowed to run twice because the guarding check and the guarding state update were not atomic with respect to the operation they were meant to protect. [3](#0-2)

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
