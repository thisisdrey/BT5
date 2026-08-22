## Title
Check-then-act race in replay-window verification allows a captured packet to be processed twice - (File: connection_state.go)

## Summary
`ConnectionState.Decrypt()` and `ConnectionState.VerifyRelay()` in `connection_state.go` split anti-replay validation into two separately-locked steps: `window.Check()` and `window.Update()`, with AEAD decryption happening *in between*, unprotected by the lock. This mirrors the root cause of the Arcadia `flashAction()` bug: a security-relevant state mutation (marking a nonce/counter as "seen") is deferred until after other work has been performed, leaving a window where the same "not yet consumed" state can be observed and acted on twice by two concurrent execution paths before either one commits the state change.

## Finding Description
`Decrypt()`:
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
``` [1](#0-0) 

`VerifyRelay()` has the identical pattern (`Check` under lock → unlock → AEAD verify → re-lock → `Update`): [2](#0-1) 

`decryptLock` is released between the `Check` and the `Update` calls, and the AEAD `DecryptDanger` call — the expensive, attacker-influenceable operation — runs entirely outside the lock. If two packets carrying the same `messageCounter` (i.e., a replayed/duplicated UDP datagram) are handed to two different reader goroutines at roughly the same time, both can call `window.Check()` before either calls `window.Update()`. Because `Check` is a pure read against the current window state and does not itself record the counter as seen, both goroutines observe `result == true` and both proceed to decrypt and hand the packet up to `handleOutsideMessagePacket` / `handleOutsideRelayPacket`, i.e. both are treated as legitimately fresh, in-order data. Only after both decryptions complete does either call `Update`, and by then the packet has already been processed twice.

This is structurally the same class of bug as the Arcadia report: the code performs a "read intermediate state, do expensive/externally-observable work, then commit state" sequence instead of an atomic "check-and-commit," and an attacker who can trigger the intermediate window (here, via UDP packet duplication/replay racing multiple socket-reader goroutines, which nebula runs — see `listenOut`/`readOutsidePackets` in `interface.go`/`outside.go`) can make both branches proceed as if the security check had never fired for either.

`interface.go` documents multiple concurrent reader routines feeding `readOutsidePackets` , and `outside.go` calls `hostinfo.ConnectionState.Decrypt` directly from that per-packet, per-goroutine hot path [3](#0-2) , making the two-goroutine race concretely reachable by an unauthenticated network attacker (no CA-signed certificate is needed to duplicate/replay an observed UDP datagram; it only requires network position to send the same ciphertext twice at the socket).

## Impact Explanation
Successful replay of a decrypted data-plane packet allows an attacker to have a previously observed encrypted message accepted and delivered to the TUN device (or forwarded through a relay) a second time. Depending on payload semantics this can duplicate application-layer traffic, and more importantly demonstrates that the anti-replay guarantee that the tunnel relies on for security (each `messageCounter` accepted at most once) is not enforced atomically — undermining the confidentiality/integrity assumptions of the AEAD channel under concurrent packet delivery. It is a "remote state poisoning" of the replay window in the analog category permitted by scope (nonce/replay handling).

## Likelihood Explanation
Exploitability depends on winning a narrow race between two goroutines processing duplicate ciphertext for the same `messageCounter`; nebula's design of parallel outside-packet readers makes this plausible under network conditions that naturally duplicate packets (or an attacker who deliberately sends the same captured UDP datagram twice in quick succession from different source paths). The race window is bounded by one AEAD decrypt operation, which is short, so likelihood is moderate rather than trivial, but the flaw is a genuine TOCTOU defect in the replay-protection logic rather than a theoretical concern.

## Recommendation
Make the check-and-update path atomic: hold `decryptLock` for the full duration of `Check` → `DecryptDanger` → `Update`, or perform the window update speculatively before decrypting and roll it back on decryption failure, so that no two callers can ever observe an "unseen" state for the same counter simultaneously.

## Proof of Concept
Not independently reproduced in this review (no test harness was run); the race is derived directly from the code structure shown above: `Check` and `Update` calls are protected by separate, non-overlapping lock acquisitions, with the un-locked, attacker-triggerable `DecryptDanger` call sandwiched between them. A reproduction would require driving two goroutines to call `ConnectionState.Decrypt` with the same `messageCounter` concurrently (e.g., via a unit test that duplicates a captured handshake/data packet and invokes `Decrypt` from two goroutines) and observing that both return successfully instead of the second returning `ErrAlreadySeen`.

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

**File:** outside.go (L125-132)
```go

	out, err = hostinfo.ConnectionState.Decrypt(f.l, h.MessageCounter, out, packet, nb)
	if err != nil {
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("Failed to decrypt packet", "error", err, "from", via, "header", h)
		}
		return
	}
```
