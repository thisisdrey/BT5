### Title
Anti-Replay Window Check/Update Is Not Atomic, Allowing Concurrent Duplicate-Packet Replay - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt()` and `ConnectionState.VerifyRelay()` implement Nebula's data-plane replay protection using a sliding-window bitmap (`Bits`), but the "check the counter hasn't been seen" step and the "mark the counter as seen" step are two separate, independently-locked operations with decryption sandwiched in between. This is analogous to the reported bug class: an authenticated message (proof/signature/counter) is validated but not atomically "consumed" before the privileged action (decryption + delivery to the firewall/TUN) is performed, allowing the same authenticated unit of work to be processed more than once.

### Finding Description
`ConnectionState.Decrypt` releases `decryptLock` after `window.Check()` succeeds, performs the AEAD decryption, and only re-acquires the lock afterward to call `window.Update()`, which is where the counter is actually marked as consumed: [1](#0-0) 

The same pattern exists in `VerifyRelay`: [2](#0-1) 

Because `Check()` (the "is this counter valid to use" test) and `Update()` (the "consume this counter" write) are not protected by a single critical section spanning the decrypt call, two goroutines that call `Decrypt`/`VerifyRelay` concurrently with the *same* `messageCounter` for the same `ConnectionState` can both pass `Check()` before either calls `Update()`. Both goroutines then independently run `DecryptDanger()` and, on success, both proceed to whatever the caller does with `out` (deliver to firewall and write to the TUN device, or forward/process a relay frame) before the second `Update()` call ever gets a chance to reject the duplicate.

This is reachable by an off-path attacker with no CA-signed identity: incoming UDP packets are dispatched to `Interface.readOutsidePackets`, which calls `hostinfo.ConnectionState.Decrypt` / `VerifyRelay` directly on data taken from the network with no other synchronization gate around the same `messageCounter` for a given peer's `ConnectionState`: [3](#0-2) [4](#0-3) 

Nebula reads inbound UDP packets across multiple concurrent per-queue reader routines/goroutines (see `Interface.readOutsidePackets`'s `q` parameter and its call sites in `interface.go`), so a captured/replayed ciphertext UDP datagram delivered twice in quick succession to different reader queues can race through `Check()` on both paths before `Update()` commits the counter on either. This defeats the intended purpose of the replay window (`ReplayWindow = 1024`) exactly like the report's `ResourceLockValidator`, where a valid, previously-used cryptographic proof (here: a valid AEAD-protected counter) is accepted a second time because the "mark as used" step happens too late relative to the "perform the privileged action" step.

### Impact Explanation
A network-positioned attacker who can capture or duplicate a legitimate encrypted Nebula packet (e.g., a UDP amplifier/duplicator on the path, or an attacker who floods a duplicate of a sniffed packet before an upstream NAT/router naturally de-duplicates) can, with the right timing, cause the same packet to be delivered twice to the TUN interface or twice through relay forwarding logic. Depending on the payload this can duplicate application-layer traffic, double-trigger stateful firewall/conntrack entries, or double-forward relay traffic, i.e. remote state poisoning through packet replay — the same impact class flagged in the source report (an authenticated action executed more than once because the anti-replay/consumption check is not atomic with the action it protects).

### Likelihood Explanation
Exploitation requires a race window that is normally narrow (decrypt is fast), and requires the attacker to get two copies of the exact same ciphertext to two different reader goroutines nearly simultaneously — this is achievable by a network-level duplicator/replicator or an attacker who intercepts and retransmits packets at high rate, but is not a trivial single-packet remote exploit like the original `ResourceLockValidator` bug (which had no time-window constraint at all). Likelihood is therefore moderate: the flaw is a genuine TOCTOU in the security-critical replay-window logic, but successful exploitation depends on timing/race conditions rather than being deterministic.

### Recommendation
Hold `decryptLock` for the entire `Check → Decrypt → Update` sequence in both `ConnectionState.Decrypt` and `ConnectionState.VerifyRelay`, or restructure `Bits` to offer a single atomic "reserve-then-confirm" operation so that no two concurrent calls can pass the check for the same counter before one of them commits the update. Alternatively, perform an unconditional `Update`/reservation before decrypting (rejecting immediately on failure) rather than only checking, so a duplicate counter is rejected before the second decryption/delivery can occur.

### Proof of Concept
1. Establish a Nebula tunnel between two nodes so a `ConnectionState` exists with an active replay `window`.
2. As a network-positioned attacker capable of duplicating traffic, capture one legitimate data-plane UDP packet (any `header.Message` packet) destined for a node running multiple UDP reader queues.
3. Retransmit two copies of the identical captured packet to the target almost simultaneously so they land on two different reader goroutines/queues.
4. Both goroutines call `hostinfo.ConnectionState.Decrypt` with the same `messageCounter`; because `window.Check()` is checked and released before `DecryptDanger` runs, both can pass the check, both decrypt successfully, and both packets reach `f.readers[q].Write(out)` (or the relay-forwarding path) — i.e., the same packet is processed twice despite the anti-replay window, before the second `window.Update()` call retroactively (and too late) reports `ErrAlreadySeen`. [1](#0-0) [5](#0-4)

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

**File:** connection_state.go (L84-107)
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
```

**File:** outside.go (L113-132)
```go
	// All remaining packets are encrypted
	if isMessageRelay {
		// Relay packets are special, this branch should always early-return
		if err = hostinfo.ConnectionState.VerifyRelay(f.l, h.MessageCounter, packet, nb); err != nil {
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				hostinfo.logger(f.l).Debug("Failed to verify relay packet", "error", err, "from", via, "header", h)
			}
			return
		}
		f.handleOutsideRelayPacket(hostinfo, via, out, packet, h, fwPacket, lhf, nb, q, localCache)
		return
	}

	out, err = hostinfo.ConnectionState.Decrypt(f.l, h.MessageCounter, out, packet, nb)
	if err != nil {
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("Failed to decrypt packet", "error", err, "from", via, "header", h)
		}
		return
	}
```
