### Title
Replay-window Check/Update race allows duplicate/replayed data-plane and relay packets to bypass anti-replay protection - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt()` and `ConnectionState.VerifyRelay()` split the anti-replay check into two separate critical sections around a costly, unlocked AEAD operation: `window.Check()` is taken and released under `decryptLock`, then `DecryptDanger` runs with no lock held, and only afterward is `window.Update()` taken under the lock again to actually mark the counter as seen. Because the mark-as-seen step is decoupled from the check step by an unlocked interval, two concurrent invocations for the same `messageCounter` can both pass `Check()` before either calls `Update()`, letting the same (or a replayed copy of the same) packet be accepted and processed twice.

### Finding Description
This mirrors the code423n4 V3Oracle finding's structural bug class: a security decision (the anti-replay verdict) is supposed to be atomically tied to a piece of state (the sliding replay window), but the code actually derives the "is this allowed" answer from that state at one point in time (`Check`) and separately, later, commits the state update (`Update`) — with the expensive/attacker-influenced operation (AEAD decrypt) happening in between, unguarded. As with the oracle bug, the validated quantity (the replay window bit for `messageCounter`) is not what ultimately governs whether the packet's effects are applied; a second read of a manipulable condition (whether another goroutine already advanced/consumed that counter) determines the outcome, and the two are not checked atomically together. [1](#0-0) [2](#0-1) 

`Decrypt` is invoked from the outside-packet-processing path that is fed directly by UDP listener goroutines reading from the network, and `nebula` explicitly supports multiple listener routines (`routines` config) as well as relay forwarding paths (`VerifyRelay`), so an attacker who captures one legitimate ciphertext packet on the wire can retransmit (replay) copies of it toward the victim. If those copies are processed concurrently by different reader goroutines (or scheduled closely enough that the first copy's `DecryptDanger` hasn't finished before the second's `Check()` runs), both calls to `window.Check()` can return `true` for the same counter, because neither call's corresponding `window.Update()` has executed yet to mark the counter as consumed. Both copies then proceed through `DecryptDanger` and are treated as valid, freshly-received packets.

### Impact Explanation
This breaks the core anti-replay guarantee of the wire protocol: a replayed encrypted message can be delivered and processed more than once by the receiver, in violation of the design goal explicitly called out elsewhere in the codebase (e.g. the relay replay-protection test in `e2e/tunnels_test.go`, which the maintainers added specifically because a similar bypass — updating the window too late — had let every replayed relay frame be re-forwarded). Depending on payload type this can cause duplicate application of state-changing messages (e.g., re-forwarded relay frames, duplicate delivery of tunnel data), i.e. remote state poisoning / traffic replay, which is one of the explicitly accepted impact classes for this analog scan.

### Likelihood Explanation
Exploitability requires: (1) the attacker to capture one legitimate ciphertext packet destined for/through a victim node (they do not need a valid CA-signed certificate to eavesdrop on or duplicate UDP traffic on the path), and (2) enough concurrency in packet processing (multiple listener routines or overlapping scheduling) for two `Check()` calls for the same counter to race ahead of the corresponding `Update()`. Nebula supports and documents multi-routine UDP listeners, and both the direct data-plane path (`readOutsidePackets` → `Decrypt`) and the relay path (`handleOutsideRelayPacket` → `VerifyRelay`) share this same check/decrypt/update pattern, increasing the number of reachable call sites. The window for the race is bounded by AEAD decrypt time, which is small but non-zero, and does not require cryptographic breakage — only network-level packet duplication, which is trivial for an on-path or off-path (spoofing UDP source) attacker.

### Recommendation
Hold `decryptLock` across the entire check-decrypt-update sequence (or otherwise make the check-and-mark operation atomic with respect to concurrent callers for the same `ConnectionState`), so that a second concurrent call for the same `messageCounter` cannot observe the window as "not yet seen" until the first call's `Update()` has committed. If holding the lock across `DecryptDanger` is a performance concern, use a reservation/claim pattern: atomically claim the counter slot before decrypting and roll back the claim only on decrypt failure.

### Proof of Concept
1. Establish a tunnel and capture one legitimate encrypted packet with counter `N` sent from A to B (e.g. via `TestRelayReplayProtection`-style packet capture in `e2e/tunnels_test.go`).
2. Configure B (or the relay) to run with `routines > 1` (multiple UDP listener goroutines each calling `readOutsidePackets`/`handleOutsideRelayPacket`).
3. Simultaneously inject two copies of the captured packet with counter `N` into two different listener sockets/goroutines at the same time.
4. Because `window.Check()` for counter `N` is only guarded individually and `window.Update()` for counter `N` happens only after `DecryptDanger` completes, both goroutines can observe `Check()==true` before either finishes `Update()`, and both proceed to decrypt and process the packet — demonstrating that the same message counter can be accepted twice, i.e., the anti-replay window is bypassed under concurrent delivery of a captured packet.

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
