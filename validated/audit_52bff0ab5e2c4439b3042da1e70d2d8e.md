### Title
Replay-Window Check/Decrypt/Update Race Allows Concurrent AEAD Cipher State Access - ([File: connection_state.go])

### Summary
`ConnectionState.Decrypt` splits the anti-replay logic into three separate steps — `window.Check()` (locked), the AEAD `DecryptDanger()` call (unlocked), and `window.Update()` (locked again) — with the mutex released for the entire decryption operation. This TOCTOU gap allows two packets carrying the same message counter (e.g., a captured/replayed ciphertext delivered twice, which requires no CA-signed certificate at all, only network visibility into an existing tunnel) to be concurrently decrypted against the same `ConnectionState.dKey` before the replay window is authoritatively updated. [1](#0-0) 

### Finding Description
`Decrypt` acquires `decryptLock`, calls `cs.window.Check(l, messageCounter)`, releases the lock, then calls `cs.dKey.DecryptDanger(...)` entirely outside the lock, and only re-acquires the lock afterward to call `cs.window.Update(...)`: [1](#0-0) 

`window.Check` only tests whether the counter looks unseen; it does not mark it as consumed — that only happens in the separate, later `Update` call under a second, independent lock acquisition: [2](#0-1) 

Because the lock is dropped between `Check` and `Update`, and the actual `DecryptDanger` call on the shared, per-tunnel `dKey` (`noiseutil.CipherState`) happens unsynchronized between those two locked sections, two packets that arrive on different reader goroutines with the *same* message counter (e.g., an attacker capturing one legitimate ciphertext off the wire and re-injecting it a second time while the original is still in flight, or simply flooding duplicate UDP datagrams) can both pass `Check`, and both proceed to call `cs.dKey.DecryptDanger` concurrently for the same `ConnectionState` before either has updated the window: [3](#0-2) 

`CipherState` implementations expose no synchronization guarantee of their own — they are plain wrappers around `noise.CipherState`/AEAD ciphers meant for single-threaded, sequential use per tunnel: [4](#0-3) 

This is structurally the same root-cause shape as the referenced report: a decision (which packet counter is "valid to consume") is checked against a snapshot of shared state, a slow/sensitive operation is performed using stale information, and only afterward is the authoritative state mutated — creating a window where two concurrent actors can both act on the same "locked-in" state before it is reconciled, exactly as queued-withdrawal users locked in a stale exchange rate before the ledger was updated by a slashing event.

### Impact Explanation
An attacker with no valid certificate can still observe/capture legitimate encrypted traffic on the wire (this is a VPN — ciphertext transits public/untrusted networks) and reinject a duplicate UDP datagram at a carefully timed moment relative to the original packet's arrival. If Nebula is configured with more than one UDP reader routine (multi-routine mode), the duplicate and the original can land on separate goroutines and race into `DecryptDanger` on the same `ConnectionState.dKey` concurrently, unguarded by any lock. Depending on the underlying AEAD implementation's internal buffer reuse/state, this concurrent unsynchronized access is a data race that can corrupt shared buffers or cause a crash of the tunnel process (denial of service), and in the worst case could produce corrupted plaintext being delivered to the TUN device for one of the racing calls.

### Likelihood Explanation
Triggering the race requires only capturing one ciphertext packet from an active tunnel and replaying it with tight timing relative to the original delivery — no cryptographic material, handshake participation, or CA-signed certificate is needed. The window is narrow but real, and is directly reachable by any attacker who can observe and inject UDP traffic to a node, which is inherent to Nebula's overlay network threat model.

### Recommendation
Hold `decryptLock` for the entire `Check` → `Decrypt` → `Update` sequence in `ConnectionState.Decrypt` (and the analogous `VerifyRelay` path), or otherwise ensure the AEAD decrypt call for a given `ConnectionState` cannot run concurrently with another decrypt call on the same state. Alternatively, mark the counter as provisionally consumed at `Check` time (single atomic check-and-set) instead of splitting the check and commit across two separately-locked critical sections.

### Proof of Concept
1. Establish a Nebula tunnel between two nodes with `listen.routines` > 1.
2. From a third host with no certificate, passively capture one legitimate application-data UDP packet destined to node A (attacker just needs network visibility, e.g., on a shared link or via ARP/route manipulation).
3. Immediately re-inject two copies of the captured packet to node A's UDP listener in rapid succession so they are picked up by two different reader routines.
4. Observe (via race detector / fuzzing under `-race`) that both `ConnectionState.Decrypt` calls proceed into `DecryptDanger` on the same `ConnectionState.dKey` before either has updated `cs.window`, demonstrating the unsynchronized concurrent access documented in `connection_state.go` lines 61–82.

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

**File:** outside.go (L105-132)
```go
	if len(packet) < header.Len+hostinfo.ConnectionState.dKey.Overhead() {
		f.messageMetrics.RxInvalid(1)
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			f.l.Debug("packet too small", "from", via, "length", len(packet))
		}
		return
	}

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

**File:** noiseutil/cipher_state.go (L9-27)
```go
// CipherState is the post-handshake AEAD cipher used for the data plane.
// Each supported cipher has its own concrete implementation in this package with the nonce endianness hardcoded,
// so the encrypt/decrypt fast path avoids interface dispatch on the byte order.
type CipherState interface {
	// EncryptDanger encrypts and authenticates a given payload.
	//
	// out is a destination slice to hold the output of the EncryptDanger operation.
	//   - ad is additional data, which will be authenticated and appended to out, but not encrypted.
	//   - plaintext is encrypted, authenticated and appended to out.
	//   - n is a nonce value which must never be re-used with this key.
	//   - nb is a scratch buffer used to assemble the nonce.
	EncryptDanger(out, ad, plaintext []byte, n uint64, nb []byte) ([]byte, error)

	// DecryptDanger authenticates and decrypts a given payload, with the same argument shape as EncryptDanger.
	DecryptDanger(out, ad, ciphertext []byte, n uint64, nb []byte) ([]byte, error)

	// Overhead returns the AEAD tag size, or 0 if the receiver is nil.
	Overhead() int
}
```
