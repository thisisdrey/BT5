### Title
Anti-replay window bypass via unlocked check-then-decrypt-then-update TOCTOU in `ConnectionState.Decrypt` - ([File: connection_state.go])

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` validate a message counter against the sliding-window replay tracker (`Bits.Check`) while holding `decryptLock`, then release the lock, perform the expensive AEAD decryption *outside* the lock, and only afterward re-acquire the lock to call `Bits.Update` to actually mark the counter as seen. This mirrors the reported bug class: a security-relevant "is this acceptable" check is evaluated against a view of state that has not yet accounted for an in-flight/concurrently-processing item, allowing the guarded action to be taken more than once for what should be a single-use value.

### Finding Description
`Decrypt` performs:
1. `decryptLock.Lock(); result := cs.window.Check(l, messageCounter); decryptLock.Unlock()`
2. `cs.dKey.DecryptDanger(...)` — expensive AEAD decryption, performed with the lock released
3. `decryptLock.Lock(); result = cs.window.Update(l, messageCounter); decryptLock.Unlock()` [1](#0-0) 

The same pattern is used in `VerifyRelay`: [2](#0-1) 

`Bits.Check` only inspects whether the counter is *marked* seen — it does not know that another goroutine is currently *in the process of* validating and decrypting the same counter, because that in-flight attempt hasn't called `Update` yet. [3](#0-2) 

If two copies of the exact same on-wire packet (identical `messageCounter`) are delivered to `readOutsidePackets`/`handleOutsideMessagePacket` concurrently — which is architecturally possible since Nebula reads UDP packets across `routines` reader goroutines/batches (`InterfaceConfig.routines`, `listen.batch`) feeding into per-hostinfo `ConnectionState` — both goroutines can call `Check` before either calls `Update`. Both will observe "not yet seen" and proceed to `DecryptDanger` and `Update` sequentially; both decrypt successfully (AES-GCM/ChaCha20-Poly1305 with the same counter/nonce just repeats the same keystream and produces the same plaintext) and both are accepted as new, distinct data-plane events, i.e. the replay window's guarantee "no counter is processed twice" is violated exactly at the same point the target-weight report describes: the accept/reject decision is made against a stale snapshot of "already-processed" state that ignores a concurrently in-flight identical unit of work.

This differs from a benign duplicate-drop scenario: here *both* copies are delivered to the TUN device / relay-forward path as legitimate, which is a replay of already-authenticated ciphertext succeeding a second time, defeating the explicit purpose of `Bits` ("anti-replay window").

### Impact Explanation
Anti-replay protection is a core security guarantee of the Noise-based transport (the report's own docs describe `window`/`Bits` explicitly as the anti-replay tracker for the data plane and relay frames, and a dedicated end-to-end test — `TestRelayReplayProtection` — exists specifically to assert replayed relay frames must be dropped). Bypassing it allows an on-path or off-path attacker capable of duplicating UDP datagrams (a passive network attacker who never holds a CA-signed certificate; they simply capture and re-inject the encrypted UDP payload) to cause an already-delivered application-layer packet to be delivered/forwarded a second time to the TUN device or, in the relay case, forwarded a second time toward the relay target — a concrete traffic replay/forgery impact.

### Likelihood Explanation
Likelihood is Medium: the race window is narrow (must win a race across the AEAD decrypt duration) but it is deterministically reachable by an attacker who duplicates a captured UDP datagram and floods it at the target's UDP listener; with multiple `routines`/batched reads (`listen.batch`, `tun.routines`), duplicate copies of the same physical packet can genuinely be scheduled onto different goroutines that then race through `Decrypt` concurrently before the shared `window.Update` closes the gap.

### Recommendation
Hold `decryptLock` for the entire `Check → Decrypt → Update` sequence (or perform an atomic "check-and-provisionally-mark" step before decryption, rolling back on decryption failure) so that no second goroutine can observe "not yet seen" for a counter that is already being processed. Apply the same fix to both `Decrypt` and `VerifyRelay`.

### Proof of Concept
1. Establish a tunnel between two Nebula nodes so `ConnectionState.dKey`/`window` are initialized. [4](#0-3) 
2. Capture one legitimate encrypted data-plane (or relay) UDP packet with message counter `N`.
3. Inject two (or more) copies of the identical captured packet into the victim's UDP receive path at effectively the same time, exploiting the multi-goroutine/batched reader path (`InterfaceConfig.routines`, `listen.batch`) so they are dispatched to `handleOutsideMessagePacket`/`handleOutsideRelayPacket` concurrently.
4. Because `Check` is evaluated and released before `Update` is called ( [1](#0-0) ), both copies can pass `Check` before either calls `Update`, causing both to be decrypted and delivered/forwarded — observable as duplicate writes to the TUN device or duplicate relay forwards (the existing `TestRelayReplayProtection` test demonstrates the expected single-forward behavior that this race can violate under concurrent delivery). [5](#0-4)

### Citations

**File:** connection_state.go (L29-47)
```go
// newConnectionStateFromResult builds a fully-populated ConnectionState from a
// completed handshake.Result. It seeds messageCounter and the replay window so
// that the post-handshake message indices already used on the wire don't count
// as missed traffic in the data plane.
func newConnectionStateFromResult(r *handshake.Result) *ConnectionState {
	ci := &ConnectionState{
		myCert:    r.MyCert,
		initiator: r.Initiator,
		peerCert:  r.RemoteCert,
		eKey:      noiseutil.NewCipherState(r.EKey, r.Cipher),
		dKey:      noiseutil.NewCipherState(r.DKey, r.Cipher),
		window:    NewBits(ReplayWindow),
	}
	ci.messageCounter.Add(r.MessageIndex)
	for i := uint64(1); i <= r.MessageIndex; i++ {
		ci.window.Update(nil, i)
	}
	return ci
}
```

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

**File:** e2e/tunnels_test.go (L377-382)
```go
// TestRelayReplayProtection asserts that a relay (forwarding-type) node rejects
// replayed relay frames. A captured relay frame, re-injected with the same
// message counter, must be dropped by the replay window rather than re-forwarded
// to the relay target. Before the fix, handleOutsideRelayPacket authenticated the
// frame but never advanced the replay window, so every replay was re-forwarded.
func TestRelayReplayProtection(t *testing.T) {
```
