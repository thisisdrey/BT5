### Title
Replay-window TOCTOU allows a duplicated/replayed data-plane or relay message to be decrypted twice - (File: connection_state.go)

### Summary
`L1ECOBridge` used a bridge-cached `inflationMultiplier` that could go stale relative to the token's live multiplier because the value was read/used across two unsynchronized operations (`transferFrom` then multiply), letting an attacker exploit the window between "state read" and "state committed" for profit. The same class of bug — a security-relevant value is *checked*, an expensive/attacker-controlled operation is performed, and only *afterward* is the checked value durably recorded — exists in Nebula's anti-replay logic in `ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` in `connection_state.go`.

### Finding Description
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` implement anti-replay in three separate, non-atomic steps:

```go
cs.decryptLock.Lock()
result := cs.window.Check(l, messageCounter)   // 1. check (holds lock briefly)
cs.decryptLock.Unlock()
if !result { return nil, ErrAlreadySeen }

out, err = cs.dKey.DecryptDanger(...)          // 2. expensive AEAD decrypt (NO lock held)

cs.decryptLock.Lock()
result = cs.window.Update(l, messageCounter)   // 3. commit/mark as seen
cs.decryptLock.Unlock()
``` [1](#0-0) [2](#0-1) 

`Bits.Check` is a pure read of the anti-replay sliding window and does not mark the counter as seen; only `Bits.Update` durably records it: [3](#0-2) [4](#0-3) 

This is structurally identical to the ECO bug: `Check` is the "read of the multiplier," `DecryptDanger` is the "transferFrom that must be consistent with that read," and `Update` is the "write-back of the multiplier." Between step 1 and step 3 there is a window during which the counter has been observed as "not yet seen" but is not yet committed as "seen." Nebula's UDP ingestion path dispatches packets to `readOutsidePackets` from multiple listener routines/goroutines (`f.handshakeManager.HandleIncoming`, and for the data/relay path down into `outside.go`'s per-message handling) [5](#0-4) , so nothing prevents two goroutines from concurrently processing two copies of the exact same wire packet (an attacker-injected duplicate of a captured, legitimate ciphertext) against the same `ConnectionState`. If both goroutines call `Check` before either calls `Update`, both will pass the replay check, both will successfully run `DecryptDanger` with the same `messageCounter` (the AEAD nonce/counter is attacker/network-replayable, not secret), and both will deliver the decrypted payload — i.e., the same message is processed twice despite the anti-replay window being designed to allow exactly one delivery per counter.

The regression risk this class of bug creates was explicit enough that the project already fixed one variant for relayed frames per the changelog: "Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them" (#1751) and "Lock replay window updates so concurrent readers can't corrupt it" (#1802) [6](#0-5) , and there is a dedicated regression test asserting relay replay protection [7](#0-6) . However, those fixes addressed forwarding/locking of the *window structure itself* — they did not close the Check→Decrypt→Update race window inherent to splitting the check and the commit around the decrypt call.

### Impact Explanation
An attacker with no CA-signed certificate, sitting on the network path (able to duplicate/inject UDP datagrams, which Nebula's own threat model already treats as achievable since it explicitly guards against replay), can cause a single captured ciphertext to be decrypted and delivered to the tun device (or forwarded, in the relay case) more than once. This directly undermines the anti-replay guarantee that `ConnectionState`/`Bits` is designed to provide, which is one of the categories explicitly in scope (nonce/replay handling, traffic replay). Depending on the payload, duplicate delivery can cause application-level side effects (duplicate processing of idempotency-sensitive traffic) or, for relayed traffic, duplicate re-forwarding load through a relay node.

### Likelihood Explanation
The race requires the attacker to get two copies of the same encrypted packet processed concurrently by two different reader goroutines before the first `Update` call completes — a narrow timing window, but one that is directly attacker-triggerable (send duplicate UDP datagrams back-to-back) and does not require compromising any cryptographic material. Likelihood is best characterized as low-to-moderate: reliably winning the race depends on scheduler/timing behavior of the specific `listen.routines`/relay configuration, but the attacker fully controls the trigger (packet duplication) and needs no certificate or prior trust.

### Recommendation
Make the anti-replay check-and-mark atomic with respect to the decrypt operation: perform `Check` and `Update` under the same critical section that also gates entry into `DecryptDanger` for a given `messageCounter`, e.g., reserve the counter (mark it provisionally "in-flight") inside the lock before decrypting, and only release/roll back the reservation after decrypt succeeds or fails, so a second concurrent copy of the same counter is rejected at `Check`/reservation time rather than being allowed to race through decryption.

### Proof of Concept
Not independently reproducible from the index alone — confirming an actual double-delivery requires running two goroutines concurrently against a live `ConnectionState.Decrypt` call with a captured duplicate packet and precise timing control around the unlock/decrypt/lock window shown in `connection_state.go` lines 61–82. This is inferred purely from the code structure (Check → unlock → Decrypt → lock → Update) and the multi-goroutine UDP ingestion path; I was not able to execute or trace runtime scheduling to confirm the race is practically winnable, so treat the likelihood assessment above as a structural/code-review conclusion rather than an empirically verified exploit.

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

**File:** outside.go (L25-80)
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

	if h.Version != header.Version {
		f.messageMetrics.RxInvalid(1)
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			f.l.Debug("Unexpected header version received", "from", via)
		}
		return
	}

	// Check before processing to see if this is a expected type/subtype
	if !h.IsValidSubType() {
		f.messageMetrics.RxInvalid(1)
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			f.l.Debug("Unexpected packet received", "from", via)
		}
		return
	}

	if !via.IsRelayed {
		if f.myVpnNetworksTable.Contains(via.UdpAddr.Addr()) {
			f.messageMetrics.RxInvalid(1)
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				f.l.Debug("Refusing to process double encrypted packet", "from", via)
			}
			return
		}
	}

	// don't keep Rx metrics for message type, since you can see those in the tun metrics
	if h.Type != header.Message {
		f.messageMetrics.Rx(h.Type, h.Subtype, 1)
	}

	// Unencrypted packets
	switch h.Type {
	case header.Handshake:
		f.handshakeManager.HandleIncoming(via, packet, h)
		return

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
