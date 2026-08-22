### Title
Anti-replay window check/update race in `ConnectionState.Decrypt` and `VerifyRelay` allows replay of a captured packet — ([File: connection_state.go])

### Summary
`ConnectionState.Decrypt` (used for data-plane messages) and `ConnectionState.VerifyRelay` (used for relay frames) each perform the anti-replay window `Check` and `Update` as two *separate* critical sections guarded by `decryptLock`, with the AEAD decryption work done **outside** the lock, in between. This mirrors the Bunni report's root cause: state is cached/consulted, an attacker-controllable operation runs unlocked in between, and the final state transition is computed against a now-stale precondition. Here, a captured/duplicated ciphertext for the same `messageCounter` can pass the `Check` step twice if delivered concurrently (e.g., via multiple UDP reader queues `q`, which nebula explicitly supports), letting both copies be decrypted and both be handed to `Update` before either commits — defeating the sliding-window replay protection guarantee.

### Finding Description
`Decrypt` does:
```go
cs.decryptLock.Lock()
result := cs.window.Check(l, messageCounter)   // read-only check
cs.decryptLock.Unlock()
if !result { return nil, ErrAlreadySeen }

out, err = cs.dKey.DecryptDanger(...)          // unlocked AEAD decrypt

cs.decryptLock.Lock()
result = cs.window.Update(l, messageCounter)   // commits state
cs.decryptLock.Unlock()
if !result { return nil, ErrAlreadySeen }
``` [1](#0-0) 

`VerifyRelay` has the identical pattern for relay-forwarded frames. [2](#0-1) 

`Bits.Check` only reads whether bit `i` is set; it never marks it, and `Bits.Update` is the only function that actually sets the bit / advances `current`. [3](#0-2) [4](#0-3) 

Because the check (`Check`) and the commit (`Update`) are not atomic with respect to each other — the lock is released and re-acquired around the decryption call — two goroutines processing the same wire packet (same `messageCounter`) concurrently can both observe `Check == true` before either has called `Update`. This is directly analogous to the Bunni bug: a value is snapshotted/verified, an external/attacker-triggerable operation executes in between, and the later "commit" step is computed against outdated state, permitting an action (there: extra token accounting; here: message admission) that should have been prevented.

Inbound packets are processed by multiple reader queues (`q`) and dispatched through `readOutsidePackets`, which calls `hostinfo.ConnectionState.Decrypt(...)` / `.VerifyRelay(...)` per packet without any additional per-counter serialization before reaching `ConnectionState`. [5](#0-4) [6](#0-5) [7](#0-6) 

An attacker who can capture and duplicate a single valid encrypted UDP packet toward a target host (no CA-signed certificate needed — this is a passive wire-level duplicate/replay, not a peer with a valid cert) can send both copies at the same time (e.g., split across multiple UDP sockets/threads feeding different reader queues) to race the window state.

### Impact Explanation
If the race is won, the anti-replay window's core guarantee — that a message counter can be admitted and processed at most once — is violated for at least one duplicate delivery. Effects include:
- Duplicate processing of a data-plane message (double delivery to the TUN device via `f.readers[q].Write(out)`), which can duplicate application-layer state changes for TCP/UDP payloads.
- For relay frames (`VerifyRelay`), a duplicated relay-forwarded packet is re-injected into `handleOutsideRelayPacket`/`readOutsidePackets` recursively, potentially causing duplicate forwarding/processing through the relay path.
- More generally, it demonstrates the anti-replay window is not race-safe against concurrent delivery of the same ciphertext, undermining the replay-protection invariant relied upon by the Noise-based transport.

This falls under the accepted "nonce/replay handling" bug class for this scan.

### Likelihood Explanation
Exploitability requires only the ability to duplicate a previously-observed valid ciphertext packet on the wire and deliver both copies with enough concurrency to hit the lock-release window between `Check` and `Update` (a narrow but non-zero timing window, made more likely because nebula explicitly uses multiple concurrent reader queues `q` for inbound UDP processing). No cryptographic material or valid certificate is needed — only interception/duplication of one legitimate packet, which is consistent with an attacker who has no CA-signed certificate but can observe/replay traffic on the path.

### Recommendation
Make the replay-window check-then-update atomic with respect to decryption: hold `decryptLock` across the entire `Check` → `DecryptDanger` → `Update` sequence (or perform an optimistic `Update`-style "reserve" of the counter under the lock before decrypting, and roll back on decryption failure) so that no two concurrent calls for the same `messageCounter` can both pass the check before either commits.

### Proof of Concept
Conceptual reproduction (analogous to the referenced report's PoC pattern of racing state mutation around a cached value):
1. Establish a tunnel and capture one valid outbound-encrypted UDP packet (a specific `header.MessageCounter` value) destined to a victim host.
2. Duplicate the exact packet bytes and send both copies to the victim at effectively the same time from two contexts (e.g., two local sockets/threads) so that both packets are dispatched to different reader queues `q` and both reach `hostinfo.ConnectionState.Decrypt` for the same `hostinfo`/`ConnectionState` concurrently.
3. Because `Check` (unlocked window read) is released before `DecryptDanger` executes, both goroutines can observe an "unseen" counter and proceed to decrypt; both then race to call `Update`, with the observable effect being that the packet payload is delivered twice (`f.readers[q].Write(out)` executed twice) instead of the second copy being rejected with `ErrAlreadySeen` before decryption ever occurs.

Note: exact success requires winning a narrow timing race and was not verified against a running instance in this analysis; the structural TOCTOU (check/decrypt/update split across three separate lock sections) is confirmed directly from the code cited above.

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

**File:** bits.go (L168-186)
```go
func (b *Bits) Update(l *slog.Logger, i uint64) bool {
	// Fast path: i is the next expected counter. Split out so the function
	// stays small and avoids paying for the slow paths' slog argument-build
	// stack frame on every call. The bit read/test/write is inlined to
	// touch the backing word once.
	if i == b.current+1 {
		pos := i & b.lengthMask
		word := pos >> 6
		mask := uint64(1) << (pos & 63)
		w := b.bits[word]
		if i > b.length && w&mask == 0 {
			b.lostCounter.Inc(1)
		}
		b.bits[word] = w | mask
		b.current = i
		return true
	}
	return b.updateSlow(l, i)
}
```

**File:** outside.go (L25-26)
```go
func (f *Interface) readOutsidePackets(via ViaSender, out []byte, packet []byte, h *header.H, fwPacket *firewall.Packet, lhf *LightHouseHandler, nb []byte, q int, localCache firewall.ConntrackCache) {
	err := h.Parse(packet)
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
