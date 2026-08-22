Based on the investigation, the closest reachable analog in this codebase to the `[H-02]` "stuck funds due to a permissionless front-runnable balance snapshot" pattern is the anti-replay window's split `Check`→(slow AEAD decrypt)→`Update` sequence in `ConnectionState.Decrypt` / `ConnectionState.VerifyRelay`.

### Title
Anti-replay window Check/Update split allows unauthenticated replay-triggered decryption amplification - (File: `connection_state.go`)

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` split the anti-replay bitmap "commit" into two separately-locked steps with a slow, unprotected operation (AEAD decryption) happening in between, mirroring the audited bug where a permissionless claim was evaluated via a before/after balance snapshot that another actor could interleave with.

### Finding Description
`ConnectionState.Decrypt` first takes `decryptLock`, calls `cs.window.Check(l, messageCounter)` to see if the counter is unseen, and immediately releases the lock: [1](#0-0) 
It then performs the expensive `DecryptDanger` AEAD operation completely outside the lock: [2](#0-1) 
Only afterward does it re-acquire the lock and call `cs.window.Update(...)` to actually commit the counter as seen: [3](#0-2) 
`VerifyRelay` follows the identical Check → decrypt → Update pattern for relay frames: [4](#0-3) 

This is the same root-cause shape as the aura report: a guard that is supposed to be evaluated atomically across an external/slow operation is instead split into a "snapshot" step and a later "commit" step, with the slow operation (claim / AEAD decrypt) sitting unguarded in between. Just as any address could call `getReward()`/`claim()` between the strategy's balance snapshots, here any unauthenticated network attacker can resend (replay) an already-captured, byte-for-byte identical ciphertext UDP packet through Nebula's multiple concurrent packet-reader paths (`Interface.listenOut`/`listenIn` queues) before the original goroutine has reached the `Update` step, since `Check` alone does not mark the counter as consumed.

### Impact Explanation
Because `Bits.Update` re-validates and is the sole authoritative "commit" (it will correctly reject the loser as a duplicate), this does not produce a full replay bypass of application traffic. However, it does allow a remote, unauthenticated attacker who has captured a single legitimate ciphertext packet off the wire (no CA-signed certificate required — capturing/replaying wire bytes needs no cryptographic material) to force the victim to redundantly perform full AEAD decryption operations for each duplicate injected during the Check→Update gap, since `Check` alone cannot block concurrent duplicates from entering the expensive decrypt path. This is a CPU-amplification / resource-exhaustion vector triggerable purely from the network path, analogous to the reward tokens being miscounted due to the un-atomic snapshot-then-commit pattern in the original report.

### Likelihood Explanation
Exploitability requires only capturing one legitimate packet (trivial for any on-path or off-path attacker who can observe/replay UDP traffic) and firing multiple copies of it in quick succession to race the unlocked window between `Check` and `Update`. No handshake, certificate, or CA trust is required to mount this — it is a pure packet/nonce-handling weakness reachable pre-authentication from the attacker's perspective (the attacker never needs their own valid cert; they are just replaying someone else's captured bytes).

### Recommendation
Collapse `Check` and `Update` into a single atomic operation performed under one held lock that "reserves" the counter before decryption begins and rolls it back only if decryption fails, rather than checking, releasing, decrypting, and separately re-checking/committing afterward — mirroring the aura fix of moving from a delta-across-an-external-call to a single atomic absolute measurement. Concretely, `Bits` should expose a single `CheckAndReserve`/`TestAndSet`-style call taken under one lock acquisition that spans the reservation, and only release/undo the reservation if `DecryptDanger` fails.

### Proof of Concept
1. Establish a legitimate tunnel and capture one data-plane UDP packet (ciphertext) sent to a Nebula node.
2. Concurrently send N duplicate copies of that exact packet to the node's UDP listener(s) at (approximately) the same time.
3. Because `Check` (line 64) is released before `DecryptDanger` (line 70) runs, multiple goroutines processing the duplicates can all pass `Check` and independently execute the full `DecryptDanger` AEAD operation before any of them calls `Update` (line 76) to mark the counter consumed; only the `Update` step (not `Check`) ultimately deduplicates, after the decrypt cost has already been paid N times. [5](#0-4)

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
