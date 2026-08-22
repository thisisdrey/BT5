### Title
Time-of-check/time-of-use race in the AEAD replay window allows duplicate decryption and delivery of a single captured packet - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt` (and its relay counterpart `VerifyRelay`) split the anti-replay window check and its corresponding update into two separately-locked critical sections with the actual AEAD decryption running unlocked in between. This is the same Checks-Effects-Interactions violation described in the external report: the "effect" that finalizes acceptance of a message counter (`window.Update`) is committed only after the risky operation (`DecryptDanger`) has already run, rather than atomically with the check. Because Nebula's UDP interface can run with multiple reader goroutines (`routines`/multi-queue mode), two goroutines can race a duplicated ciphertext packet through this window and both pass the `Check`, both successfully decrypt, and both proceed to deliver/process the same message before either commits the `Update`.

### Finding Description
`Decrypt` in `connection_state.go` performs:
1. Lock, `window.Check(messageCounter)`, Unlock — external "check" step.
2. `dKey.DecryptDanger(...)` — the expensive/"interaction" step, executed **without holding `decryptLock`**.
3. Lock, `window.Update(messageCounter)`, Unlock — the "effect" that actually marks the counter as consumed. [1](#0-0) 

The identical pattern exists in `VerifyRelay`, used for relayed traffic: [2](#0-1) 

Between steps 1 and 3 there is no lock held, so if an attacker (a network-position/on-path attacker who can duplicate or replay a previously observed UDP datagram — they need no CA-signed certificate, since the ciphertext is simply being duplicated, not forged) sends two copies of the same ciphertext packet to arrive at (approximately) the same time, both goroutines can:
- see `window.Check` return `true` (the counter has not been marked yet),
- both successfully run `DecryptDanger` (the ciphertext is identical, so both succeed),
- both proceed past the check and only afterward serialize on `window.Update`.

This defeats the purpose of the replay window for concurrent delivery, and the surrounding caller code, `readOutsidePackets`, does not re-check anything before dispatching the decrypted payload: [3](#0-2) 

Nebula's interface can be configured to run multiple UDP reader goroutines (`routines`), all sharing the same `HostInfo`/`ConnectionState` for a given peer, which is the concrete mechanism that makes the race reachable in production rather than purely theoretical: [4](#0-3) [5](#0-4) 

Notably, the project's own changelog shows this exact code was previously *unsafe against concurrent readers* and was "fixed" by adding locking around the bit-window mutation itself (#1802) and around relay forwarding (#1751) — but neither fix closed the check→decrypt→update gap; it only made the individual `Check`/`Update` calls internally consistent: [6](#0-5) 

This mirrors the GClaimManager report precisely: the state-mutating "effect" (burn / window.Update) happens at the very end, after external-facing work (external calls / DecryptDanger) has already been performed, so a second concurrent invocation can slip through the check before the first invocation's effect lands.

### Impact Explanation
This is a concrete break of nebula's nonce/replay-protection guarantee under legitimate operating conditions (multi-queue UDP listening, which is a normal and documented configuration, not a corner case). An attacker capable of duplicating one legitimately captured ciphertext datagram (trivial for anyone with visibility into the underlay path, e.g. a NAT/router in the path, or simply causing UDP duplication) can cause that single packet to be decrypted and delivered twice to the tun device / control-plane handler, i.e. traffic/message replay despite AEAD nonce protection. Depending on payload type this can duplicate application-layer traffic, double-process `header.Control` relay-management messages, or double-trigger roaming/connection-manager side effects — all without needing a valid certificate or completing any handshake, since the attacker only needs to duplicate someone else's already-authenticated ciphertext.

### Likelihood Explanation
Likelihood is moderate: it requires (a) the target to run with `routines > 1` (multi-queue), which is a supported and documented configuration for busy tunnels, and (b) the attacker to duplicate a UDP packet so both copies are processed concurrently by different reader goroutines — something achievable by an on-path attacker or even by natural UDP duplication in unreliable network paths. No cryptographic material or valid certificate is required, only the ability to capture/duplicate ciphertext in flight.

### Recommendation
Make the check-decrypt-update sequence atomic with respect to a given `ConnectionState`/message counter: hold `decryptLock` for the entire `Check → DecryptDanger → Update` sequence (or perform an atomic check-and-reserve of the counter before decrypting, and only release/rollback the reservation if decryption fails), instead of releasing the lock between the check and the update. Apply the same fix to `VerifyRelay`. This closes the gap without requiring a general `nonReentrant` primitive by ensuring only one goroutine can be mid-flight for any duplicate counter at a time.

### Proof of Concept
1. Configure two nebula peers with `listen.routines` (or the equivalent multi-queue setting) set to a value > 1 so multiple reader goroutines process inbound UDP packets for the same tunnel concurrently.
2. Establish a tunnel and capture one legitimate data-plane UDP packet (ciphertext) sent from peer A to peer B.
3. Rapidly re-inject (duplicate) that exact captured UDP packet at peer B twice, timed so both copies are picked up by two different reader goroutines before either finishes processing (i.e., before `window.Update` runs for the first copy).
4. Observe that both copies pass `ConnectionState.Decrypt`'s `window.Check`, both successfully decrypt via `DecryptDanger`, and both payloads get delivered to `f.readers[q].Write(out)` (or, for relay/control traffic, both trigger `handleOutsideRelayPacket` / control-message handling) — i.e., the same message is processed twice despite replay-window protection.

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

**File:** outside.go (L124-146)
```go
	}

	out, err = hostinfo.ConnectionState.Decrypt(f.l, h.MessageCounter, out, packet, nb)
	if err != nil {
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("Failed to decrypt packet", "error", err, "from", via, "header", h)
		}
		return
	}

	// Roam before we respond
	f.handleHostRoaming(hostinfo, via)
	f.connectionManager.In(hostinfo)

	switch h.Type {
	case header.Message:
		switch h.Subtype {
		case header.MessageNone:
			f.handleOutsideMessagePacket(hostinfo, out, packet, fwPacket, nb, q, localCache)
		default:
			hostinfo.logger(f.l).Error("IsValidSubType was true, but unexpected message subtype seen", "from", via, "header", h)
			return
		}
```

**File:** interface.go (L41-41)
```go
	routines           int
```

**File:** interface.go (L73-73)
```go
	routines              int
```

**File:** CHANGELOG.md (L79-81)
```markdown
- Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them. (#1751)
- Fix a race in relay state handling. (#1753)
- Lock replay window updates so concurrent readers can't corrupt it. (#1802)
```
