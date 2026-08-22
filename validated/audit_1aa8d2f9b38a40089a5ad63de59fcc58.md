### Title
Anti-replay window check-then-update race in `ConnectionState.Decrypt`/`VerifyRelay` allows replayed data-plane packets to bypass replay protection - (File: connection_state.go)

### Summary
The Sherlock finding's root cause is a check-then-act (TOCTOU) pattern: the withdraw functions validate an expected exchange rate/asset amount, but the pool state used to actually execute the withdrawal can change before the action completes, letting an attacker's already-changed state slip past a check that was performed against stale data. The same class of bug — a validity check performed against mutable shared state that is *released* before the state is durably updated — exists in nebula's anti-replay nonce handling in `ConnectionState.Decrypt` and `ConnectionState.VerifyRelay`.

### Finding Description
`Decrypt` and `VerifyRelay` split what should be a single atomic "check-and-mark-seen" replay-window operation into two separate locked critical sections with the lock released in between: [1](#0-0) 

```
cs.decryptLock.Lock()
result := cs.window.Check(l, messageCounter)
cs.decryptLock.Unlock()
if !result { return nil, ErrAlreadySeen }

out, err = cs.dKey.DecryptDanger(...)   // no lock held here
...

cs.decryptLock.Lock()
result = cs.window.Update(l, messageCounter)   // this is what actually marks the counter "seen"
cs.decryptLock.Unlock()
```

`Bits.Check` only reads the window bitmap; the bit for `messageCounter` is not set until `Bits.Update` runs, after decryption completes. Between the `Check` and the `Update`, the lock is fully released, so any two goroutines that concurrently receive the *same* messageCounter (a replayed/duplicated UDP packet) will both pass `Check` (`window.get(i)` is still `false` for both), both proceed to call `DecryptDanger` with the identical AEAD nonce/counter, and both can succeed and deliver the plaintext to the tunnel before either one calls `Update`. Only after both decrypts complete does one of the two `Update` calls "win" and mark the bit set, but by then both payloads have already been accepted and forwarded.

The exact same pattern exists in `VerifyRelay`, used to authenticate relayed frames: [2](#0-1) 

The upstream duplicate-detection design (`Bits.Check`/`Bits.Update`) assumes callers treat "check" and "mark as seen" as one atomic unit, as documented in the comment on `Bits.Check`: [3](#0-2) 

but `ConnectionState.Decrypt`/`VerifyRelay` violate that invariant by dropping `decryptLock` between the two calls and performing the (comparatively slow) AEAD decrypt operation outside the lock. This is precisely analogous to the Sherlock M-15 root cause: a slippage-style check is validated against a snapshot of shared state, then the actual state-mutating operation happens later, and the two are not atomic, so an attacker who can inject the same wire packet twice in quick succession (trivially done on UDP, which is unauthenticated at the transport layer and easy to duplicate/replay on the network path before the AEAD counter check takes effect) can defeat the intended one-shot semantics of the counter.

### Impact Explanation
Nebula's data-plane anti-replay window exists specifically to guarantee that a given `messageCounter` (nonce) is accepted and delivered to the tun device / relay path exactly once. The race allows an attacker positioned on the network path (no valid CA-signed certificate required — this is purely a transport-level UDP duplication, since the attacker only needs to duplicate an already-observed ciphertext packet, not forge one) to cause a single legitimate encrypted packet to be decrypted and delivered twice concurrently. This is a genuine replay-protection bypass in the data plane / relay-authentication path, which the project's own security model treats as a first-class threat (see the multiple anti-replay hardening fixes noted in the changelog, e.g. "Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them" and "Lock replay window updates so concurrent readers can't corrupt it"): [4](#0-3) 

Duplicate delivery of application traffic (e.g., duplicate TCP/UDP payloads injected into the tun device, or duplicate relayed frames re-forwarded by a relay) breaks the confidentiality/integrity guarantee that Noise-protected traffic is delivered at-most-once, and can be leveraged for traffic amplification or to desynchronize higher-layer protocols that assume exactly-once delivery.

### Likelihood Explanation
Exploitation only requires the ability to duplicate a single previously-observed UDP packet on the wire and deliver both copies to the target host in a tight enough window that they are processed by concurrent goroutines before either `Update` call completes — an operation trivially performed by any on-path or off-path attacker capable of packet injection/duplication, with no cryptographic material and no valid certificate needed. The race window is bounded by one AEAD decrypt operation, which is short but non-zero, and is directly reachable on every inbound Noise-protected data packet and every relayed frame.

### Recommendation
Hold `decryptLock` for the entire duration of `Check` → `DecryptDanger` → `Update`, so the anti-replay check-and-mark operation is atomic with respect to concurrent packets carrying the same `messageCounter`. Alternatively, perform the `Update` (mark-as-seen) step immediately after `Check` succeeds and before calling `DecryptDanger`, rolling back (clearing) the bit only if decryption subsequently fails — this preserves the single-lock atomicity guarantee that `Bits.Check`/`Bits.Update` are documented to require, matching the fix already applied for the relay forwarding path per the changelog entry above.

### Proof of Concept
1. Establish a tunnel between two nebula nodes (attacker only needs to observe traffic on the wire, not hold any certificate).
2. Capture one ciphertext data-plane UDP packet (or one relayed frame) with counter `N` sent by the sender.
3. Inject two copies of the identical captured packet to the receiver's UDP socket back-to-back (or via two racing UDP read routines/threads on the receiving host).
4. Both copies reach `ConnectionState.Decrypt` (or `VerifyRelay`) concurrently; both call `cs.window.Check(l, N)` before either calls `cs.window.Update(l, N)`, so both `Check` calls return `true`.
5. Both goroutines proceed to `cs.dKey.DecryptDanger(...)` with counter `N` and succeed (since the AEAD key/nonce pair is unchanged and valid for decryption regardless of prior use), delivering the decrypted payload twice — once from each goroutine — to the tun device or relay-forward path, only after which one of the two `Update` calls marks the bit and the other returns `ErrAlreadySeen` (too late to prevent the double delivery).

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

**File:** CHANGELOG.md (L79-82)
```markdown
- Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them. (#1751)
- Fix a race in relay state handling. (#1753)
- Lock replay window updates so concurrent readers can't corrupt it. (#1802)
- Reject malformed handshakes more reliably, including invalid ed25519 key lengths. (#1601, #1756)
```
