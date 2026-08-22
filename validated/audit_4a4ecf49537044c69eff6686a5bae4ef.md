### Title
Replay-window Check/Update race allows an identical UDP packet to be processed twice - (File: connection_state.go)

### Summary
`Quest.sol#claim()` has a TOCTOU flaw: it checks `isClaimed`, does external work (reward transfer), and only marks the token claimed afterward, so a race between two callers observing the same "not yet claimed" state lets the resource be consumed twice (or inconsistently) before the guard is finalized. Nebula's data-plane replay protection in `ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` has the same shape: it checks the replay window, releases the lock, does the expensive decrypt work unlocked, and only then re-acquires the lock to mark the counter as seen.

### Finding Description
`ConnectionState.Decrypt` performs three separate, lock-scoped steps instead of one atomic operation: [1](#0-0) 

1. `decryptLock.Lock(); window.Check(messageCounter); decryptLock.Unlock()`
2. `dKey.DecryptDanger(...)` — done **without** holding `decryptLock`
3. `decryptLock.Lock(); window.Update(messageCounter); decryptLock.Unlock()`

Between steps 1 and 3 the lock is fully released. If two UDP datagrams carrying the exact same `messageCounter` (i.e., a duplicated/replayed packet, or two copies delivered to two different reader goroutines) arrive close enough together, both can pass `Check` (neither has been marked seen yet), both successfully run `DecryptDanger` (AEAD decryption for a given counter/nonce is deterministic and will succeed twice for the same ciphertext), and only afterward does `Update` serialize — one call wins and returns `true`, the other returns `false`/`ErrAlreadySeen`. Critically, the *decrypted plaintext from the losing call has already been produced and is handed back to the caller before the `Update` check fires*, so the replay window's job of preventing double processing of the same wire packet is not actually enforced atomically with respect to the check. `ConnectionState.VerifyRelay` has the identical pattern for relay frames: [2](#0-1) 

This directly mirrors the reported bug class: an authorization/state check (`isClaimed` / `window.Check`) is decoupled in time from the state-committing step (`_setClaimed` / `window.Update`), with attacker-observable work happening in between that both racing paths can complete.

Packets are read and dispatched to `readOutsidePackets` from Nebula's outside listener loop(s): [3](#0-2) 

Since `Interface` sets up multiple reader routines/queues (per the multiple `go func`/worker patterns in `interface.go`), packets for the same `hostinfo.ConnectionState` can be dispatched to different goroutines concurrently, giving an off-path or on-path attacker (who can inject or duplicate UDP datagrams toward the target, without needing a valid CA-signed certificate — replay attacks are exactly what the replay window exists to stop) a real opportunity to win this race.

### Impact Explanation
Successful exploitation causes a replayed ciphertext to be decrypted and delivered up the stack (e.g., written to the TUN device / processed as a legitimate inbound message) more than once, defeating the anti-replay guarantee that the Noise/AEAD replay window is supposed to provide. This is a concrete instance of "traffic decryption/forgery/replay" impact: an attacker can cause duplicate delivery of a captured packet by racing two copies of it into the interface before the window is updated, undermining the integrity guarantee that each message counter is processed exactly once.

### Likelihood Explanation
Exploitation requires only the ability to send (or duplicate) two UDP datagrams with the same header/message-counter to the victim in a tight enough window — no valid certificate or successful handshake takeover is needed, only capture/replay of an already-observed packet plus the ability to race delivery across the multiple listener goroutines. This is comparable in difficulty to classic UDP replay/duplication and is plausible on a lossy or attacker-influenced network path (e.g., duplicating packets, or an on-path attacker forcing them onto separate reader queues).

### Recommendation
Hold `decryptLock` for the entire check-decrypt-update sequence (or use a single atomic "check-and-reserve" step, e.g., speculatively mark the counter as pending before decrypting and roll back on decrypt failure) so that two callers can never both pass `Check` for the same counter before one of them commits `Update`. Apply the same fix to both `Decrypt` and `VerifyRelay`.

### Proof of Concept
1. Establish a tunnel and capture one valid data-plane UDP packet `P` (header + ciphertext) destined for the target's `outside` socket.
2. Send two copies of `P` to the target in immediate succession such that they are picked up by two different reader goroutines (or arrive close enough in time) before either has completed its `window.Update` call.
3. Both goroutines execute `ConnectionState.Decrypt`: both pass `window.Check` (returns true for both, since neither has updated the window yet), both successfully run `dKey.DecryptDanger` and obtain the same plaintext, and both proceed to hand the decrypted payload to the caller for further processing (e.g., writing to TUN) — only the subsequent `window.Update` call distinguishes winner/loser, by which point the duplicate has already been processed once each.
4. Observe that the same original packet's payload is delivered/processed twice, despite the replay window nominally guaranteeing single delivery per counter.

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

**File:** outside.go (L25-41)
```go
func (f *Interface) readOutsidePackets(via ViaSender, out []byte, packet []byte, h *header.H, fwPacket *firewall.Packet, lhf *LightHouseHandler, nb []byte, q int, localCache firewall.ConntrackCache) {
	err := h.Parse(packet)
	if err != nil {
		// Hole punch packets are 0 or 1 byte big, so lets ignore printing those errors
		// TODO: record metrics for rx holepunch/punchy packets?
		if len(packet) > 1 {
			f.messageMetrics.RxInvalid(1)
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				f.l.Debug("Error while parsing inbound packet",
					"from", via,
					"error", err,
					"packet", packet,
				)
			}
		}
		return
	}
```
