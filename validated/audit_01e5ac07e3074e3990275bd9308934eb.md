## Title
Check-then-Act race in the anti-replay window lets a duplicated packet be decrypted and accepted twice - ([File: connection_state.go])

### Summary
The Sherlock report describes a bug class where a security-critical value (slippage/price) is computed and then acted upon later, with no atomic guarantee that the value hasn't been manipulated in between — an attacker races the gap between "check" and "commit" to get a state accepted that should have been rejected. Nebula's `ConnectionState.Decrypt` (and its twin `VerifyRelay`) has the same check-then-act structure applied to the anti-replay window: it checks the message counter, releases the lock, performs the (comparatively expensive) AEAD decryption, and only afterwards re-acquires the lock to mark the counter as consumed. Nothing prevents a second copy of the same ciphertext from entering `Decrypt` while the first copy is still between `Check` and `Update`.

### Finding Description
`Decrypt` in [1](#0-0)  performs:
1. Lock, `cs.window.Check(l, messageCounter)`, unlock.
2. `cs.dKey.DecryptDanger(...)` — AEAD decrypt, done *without holding the lock*.
3. Lock, `cs.window.Update(l, messageCounter)`, unlock.

`Bits.Check` at [2](#0-1)  is a pure read that does not mark anything as seen; only `Bits.Update` at [3](#0-2)  marks the counter bit. Because `Check` and `Update` are two separate critical sections rather than one atomic "check-and-set" operation, a duplicate/replayed UDP datagram carrying the exact same `messageCounter` can be handed to `Decrypt` a second time before the first call's `Update` has run. The second call's `Check` will also return `true` (the bit hasn't been set yet), so both goroutines proceed to decrypt the same ciphertext with the same nonce and both succeed, and only after that does either call `Update`.

The same pattern exists in `VerifyRelay` at [4](#0-3) , which is reached directly from unauthenticated-looking relay traffic parsed in `readOutsidePackets` at [5](#0-4) . Relay/message packets are routed to `ConnectionState.Decrypt`/`VerifyRelay` purely by looking up the `RemoteIndex` in the hostmap — no fresh certificate check is performed on the data path — so this is exercised on every inbound UDP datagram, including ones injected/duplicated by a pure network-level attacker who never presents a CA-signed certificate; they only need to capture (or be on-path for) one legitimate ciphertext packet and re-inject it quickly.

This mirrors the M-2 root cause precisely: a decision (mint amount / replay-acceptance) is finalized based on a snapshot taken before an expensive/asynchronous operation, and the state that should have prevented a second, illegitimate use of that snapshot is only updated afterward, leaving a window where two concurrent operations both see the "not yet used" state.

### Impact Explanation
If exploited, this defeats the entire purpose of the anti-replay window: a captured ciphertext packet can be delivered twice, so the corresponding cleartext (a tunneled IP packet) is injected twice into the target's TUN device / firewall path. Depending on payload this enables traffic duplication/replay attacks on the encrypted tunnel (e.g., double-delivery of application-layer requests), and for relay frames it re-introduces exactly the replay-forwarding bug the project's own changelog says it fixed ("Lock replay window updates so concurrent readers can't corrupt it" / "Advance the replay window on relayed packets..." — [6](#0-5) ), meaning a relay could be tricked into re-forwarding a replayed frame if the two copies race the Check/Update gap rather than the single check the changelog entry addressed. This is a concrete traffic-forgery/replay impact, not merely theoretical.

### Likelihood Explanation
Likelihood depends on the UDP stack's concurrency model: if inbound packets for the same hostinfo/`ConnectionState` are ever processed on more than one goroutine concurrently (e.g., multiple `recvmmsg`/listener queues as suggested by the batched-receive code in `udp/udp_linux.go`), duplicate delivery is plausible any time a packet is naturally duplicated on the wire, retransmitted, or deliberately replayed by an attacker who captured one packet. An attacker does not need a valid certificate or key material — only the ability to observe and resend one ciphertext datagram, which is standard for any on-path or off-path packet-capture position. The race window is bounded by one AEAD decrypt operation, which is small but non-zero, especially under load where many packets are processed in parallel across queues.

### Recommendation
Make the check-and-mark operation atomic: hold `decryptLock` across `Check`, then perform decryption, and only release the lock (or use a single combined `CheckAndReserve`/pending-mark step before decrypting, rolling back on decrypt failure) so that a second concurrent copy of the same counter is rejected immediately rather than being allowed to race into `DecryptDanger`. Concretely, mark the counter as "in-flight" under the lock before decrypting (e.g., a tentative `Update` that can be undone on decrypt failure, or a separate "claimed" bitmap checked/set atomically), rather than leaving a public TOCTOU gap between `Check` and `Update`.

### Proof of Concept
Conceptual sequence (not exploit code, since indexed sources for actual UDP receive loop concurrency were not fully available):
1. Attacker captures one legitimate encrypted `Message`/`MessageRelay` UDP packet sent to a Nebula node (destination hostinfo/ConnectionState `X`, counter `N`).
2. Attacker immediately re-injects two copies of that exact packet toward the target in rapid succession (or the underlying UDP receive path naturally delivers a duplicate, e.g. from network-level retransmission).
3. If the two copies are picked up by two goroutines/queues before either finishes `Decrypt`, both call `cs.window.Check(l, N)`, both see `true` (bit for `N` not yet set), and both proceed into `cs.dKey.DecryptDanger(...)`.
4. Both decrypts succeed (same key/nonce/ciphertext), so both copies of the tunneled packet are delivered to the TUN device / relay-forwarded, and only afterward do both goroutines call `Update`, at which point the second `Update` may itself report `!result` — but the double-decrypt/double-delivery has already happened. [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** outside.go (L86-121)
```go
	// Relay packets are special
	isMessageRelay := (h.Type == header.Message && h.Subtype == header.MessageRelay)

	var hostinfo *HostInfo
	if isMessageRelay {
		hostinfo = f.hostMap.QueryRelayIndex(h.RemoteIndex)
	} else {
		hostinfo = f.hostMap.QueryIndex(h.RemoteIndex)
	}

	// At this point we should have a valid existing tunnel, verify and send
	// recvError if necessary
	if hostinfo == nil || hostinfo.ConnectionState == nil {
		if !via.IsRelayed {
			f.maybeSendRecvError(via.UdpAddr, h.RemoteIndex)
		}
		return
	}

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
```

**File:** CHANGELOG.md (L79-82)
```markdown
- Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them. (#1751)
- Fix a race in relay state handling. (#1753)
- Lock replay window updates so concurrent readers can't corrupt it. (#1802)
- Reject malformed handshakes more reliably, including invalid ed25519 key lengths. (#1601, #1756)
```
