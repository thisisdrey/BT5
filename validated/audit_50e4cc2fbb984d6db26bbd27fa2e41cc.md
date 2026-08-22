### Title
Split-window replay check allows duplicate delivery of a replayed encrypted packet - ([File: connection_state.go])

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` implement anti-replay protection as three separate, independently-locked steps: `window.Check()` (read), `dKey.DecryptDanger()` (unlocked AEAD verification), and `window.Update()` (write/commit). Because the "commit" step that actually marks a message-counter as consumed happens only *after* the packet has already been authenticated and handed off for delivery/forwarding, two concurrently-processed copies of the same captured ciphertext can both pass the `Check()` gate before either one reaches `Update()`. This mirrors the Axelar bug pattern: an irreversible, security-relevant action (packet delivery/relay-forward) is committed based on an optimistic pre-check, while the actual state-consuming operation that should prevent duplication happens later and is not atomic with the check.

### Finding Description
`Decrypt` (used for direct tunnel traffic) and `VerifyRelay` (used for relay-forwarded frames) both follow this pattern: [1](#0-0) 

```
cs.decryptLock.Lock()
result := cs.window.Check(l, messageCounter)   // read-only test
cs.decryptLock.Unlock()
if !result { return nil, ErrAlreadySeen }

out, err = cs.dKey.DecryptDanger(...)          // no lock held here
if err != nil { return nil, err }

cs.decryptLock.Lock()
result = cs.window.Update(l, messageCounter)   // the actual "commit"
cs.decryptLock.Unlock()
```

The same pattern exists in `VerifyRelay`: [2](#0-1) .

`Bits.Check` only inspects whether a counter has already been marked; it does not mark it: [3](#0-2) . Only `Bits.Update` actually sets the bit that prevents a duplicate from being accepted again: [4](#0-3) .

Because `Check` and `Update` are each protected by their own lock/unlock pair rather than one atomic critical section spanning the whole `Check → Decrypt → Update` sequence, if two goroutines process two copies of the exact same previously-transmitted ciphertext concurrently (an attacker on the network path can trivially duplicate a UDP datagram — no CA-signed certificate or private key is required to replay bytes it captured), both goroutines can:
1. Call `Check()` and get `true` (neither has updated the window yet).
2. Both successfully AEAD-decrypt the same valid ciphertext (it's a legitimate captured packet, so decryption succeeds for both).
3. Both proceed to deliver the plaintext — to the TUN device via `handleOutsideMessagePacket`, or forwarded onward via `handleOutsideRelayPacket` in the relay case (`outside.go`, lines 113–124: `VerifyRelay` succeeds then `handleOutsideRelayPacket` is invoked) [5](#0-4) .
4. Only afterward does one of the two `Update()` calls "win"; the other returns `ErrAlreadySeen`, but this happens *after* the duplicate has already been delivered/forwarded.

This is architecturally the same root-cause shape as the Axelar finding: the side-effect that matters (delivery of a message to the application, or forwarding of a relay frame to the ultimate destination) is performed based on a provisional check, while the operation meant to make that check final and prevent re-use happens later and is not part of the same atomic operation. In Axelar, tokens were burned (an action taken) before the paired action (destination execution) was confirmed, with no compensating rollback. Here, the packet is delivered/forwarded (an action taken) before the anti-replay counter update (the "confirmation" that this message hasn't been used before) is finalized, and there is no mechanism to undo the delivery/forward if the second `Update()` call determines it was a duplicate.

The project's own changelog documents that concurrent access to the replay window bitmap was previously unsafe and was partially addressed: "Lock replay window updates so concurrent readers can't corrupt it. (#1802)" [6](#0-5) . That fix appears to have added locking to prevent *data corruption* of the `Bits` structure, but the `Check`/`Update` calls remain two separate critical sections rather than one atomic operation spanning the decrypt, leaving the logical TOCTOU (duplicate acceptance/delivery race) unaddressed.

### Impact Explanation
An on-path or capturing attacker (no CA-signed certificate required — they only need to have observed one legitimate ciphertext, e.g., via network tap, and can duplicate it at the wire level) can cause a single legitimate encrypted packet to be decrypted and delivered/forwarded more than once. For the relay path (`handleOutsideRelayPacket`), this causes duplicate forwarding of a relayed message toward the destination — the exact scenario the project's own regression test (`TestRelayReplayProtection`) exists to guard against: "Before the fix, handleOutsideRelayPacket authenticated the frame but never advanced the replay window, so every replay was re-forwarded" [7](#0-6) . The race described here is a narrower, timing-dependent variant of that same class of bug: it isn't that the window is never advanced, but that the advance is not atomic with the check that gates delivery, so a race window still permits duplicate accept-and-deliver for concurrently-processed traffic. Impact: traffic replay (duplicate delivery of previously sent messages to the TUN device, or duplicate forwarding of relay frames), which can be leveraged to replay commands/messages, duplicate side effects on the receiving application, or amplify relay traffic.

### Likelihood Explanation
Likelihood is moderate and depends on Nebula's packet-processing model actually invoking `Decrypt`/`VerifyRelay` for the same `ConnectionState` concurrently from more than one goroutine (e.g., multiple UDP read queues, as suggested by the `q` queue-index parameter threaded through `readOutsidePackets`) [8](#0-7) . I was not able to confirm from the indexed code whether the UDP listener actually dispatches packets for the same underlying tunnel across multiple concurrent worker goroutines in this build, which is necessary for the race window to be practically triggerable, or whether a single goroutine per hostinfo effectively serializes calls to `Decrypt`. This is a limitation of what the current index/tooling exposed; confirming the exact reader-goroutine topology would require examining the UDP listener setup code, which the available index did not surface as clearly relevant snippets for this query.

### Recommendation
Make the replay check-and-mark operation atomic: hold `decryptLock` (or an equivalent single critical section) across the entire `Check → Decrypt → Update` sequence, or restructure `Bits` to expose a single `CheckAndReserve`/`TryUpdate` operation that atomically tests-and-sets the counter bit before the AEAD decrypt is attempted, so a second concurrent copy of the same ciphertext is rejected at the check stage rather than being allowed to decrypt and be delivered before the window is updated.

### Proof of Concept
Not independently reproducible from static analysis alone; a concrete PoC would require confirming (a) that Nebula's UDP read path dispatches packets belonging to the same `ConnectionState` across multiple concurrent goroutines/queues, and (b) racing two injected copies of a single captured ciphertext against those goroutines to observe duplicate `handleOutsideMessagePacket`/`handleOutsideRelayPacket` invocations before the second `window.Update()` call rejects the duplicate. This would need to be validated by a Devin session with test-execution access, since the static index does not let me exercise goroutine-level timing.

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

**File:** connection_state.go (L85-108)
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

**File:** outside.go (L25-25)
```go
func (f *Interface) readOutsidePackets(via ViaSender, out []byte, packet []byte, h *header.H, fwPacket *firewall.Packet, lhf *LightHouseHandler, nb []byte, q int, localCache firewall.ConntrackCache) {
```

**File:** outside.go (L113-124)
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
```

**File:** CHANGELOG.md (L79-82)
```markdown
- Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them. (#1751)
- Fix a race in relay state handling. (#1753)
- Lock replay window updates so concurrent readers can't corrupt it. (#1802)
- Reject malformed handshakes more reliably, including invalid ed25519 key lengths. (#1601, #1756)
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
