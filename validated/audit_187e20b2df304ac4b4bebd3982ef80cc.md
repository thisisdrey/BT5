### Title
Non-atomic replay-window check-then-update in `ConnectionState.Decrypt`/`VerifyRelay` allows packet replay under multi-routine operation - (File: `connection_state.go`)

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` split the sliding-window replay check (`window.Check`) and the window state update (`window.Update`) into two separate, independently-locked critical sections, with the AEAD decrypt operation running in between, unlocked. This mirrors the reported bug class: an operation that is supposed to be gated by a state-mutating check is allowed to proceed when the state mutation is deferred/decoupled from the check, so the "credit" (here, the replay-window slot) is not consumed atomically with the action it is meant to protect (here, decrypting/accepting the packet).

### Finding Description
`ConnectionState.Decrypt` performs:
1. Lock, `cs.window.Check(l, messageCounter)`, unlock.
2. If allowed, `cs.dKey.DecryptDanger(...)` (no lock held).
3. Lock, `cs.window.Update(l, messageCounter)`, unlock. [1](#0-0) 

The same pattern is used in `VerifyRelay`, which authenticates and forwards relay frames: [2](#0-1) 

Because `Check` and `Update` are each individually locked but the pair is not atomic, two goroutines processing the same `messageCounter` concurrently can both pass `Check` before either has called `Update` to mark the counter as consumed. Nebula supports running multiple independent UDP reader routines (`routines`/`listen.routines` config), each with its own goroutine calling `readOutsidePackets` → `ConnectionState.Decrypt` concurrently on the same `HostInfo`/`ConnectionState`: [3](#0-2) [4](#0-3) 

An attacker who can duplicate a captured, previously valid, still-in-window UDP packet (e.g., resend the exact same ciphertext to the listener, potentially arriving on different reader routines/sockets due to `SO_REUSEPORT`-based multi-listener setup) can race two decrypt attempts with the same `messageCounter`. Both may pass `window.Check` (since neither has yet called `window.Update`), causing the same packet to be decrypted and delivered twice, bypassing the intended replay protection that the sliding window (`Bits`) is designed to provide.

This is analogous to the reported Juicebox issue: a state mutation (`creditsOf[]` update / here, `window.Update`) that should happen atomically with the gating check is separated from it, letting the protected action occur outside the intended invariant. The `Bits.Update`/`Check` logic itself is documented as correctly maintaining lost/dupe counters when called serially: [5](#0-4) 

but that correctness assumes the caller enforces atomicity of check-then-update, which `ConnectionState.Decrypt`/`VerifyRelay` does not.

### Impact Explanation
This allows replay of already-decrypted-once traffic on established tunnels, undermining the replay-protection guarantee that Nebula's changelog explicitly calls out as security-relevant (the fix for relay-frame replay was itself a recent security fix, tracked in `TestRelayReplayProtection`): [6](#0-5) [7](#0-6) 

While that specific relay-forwarding bug is already fixed by advancing the window before returning, the underlying check/update split in `ConnectionState.Decrypt`/`VerifyRelay` remains racy whenever more than one reader routine is active, re-opening a duplicate-processing / replay window for both direct data-plane traffic and relay frames.

### Likelihood Explanation
Exploitability requires only that the deployment uses `routines > 1` (a documented, supported performance option) or otherwise allows concurrent delivery of duplicate UDP datagrams to the process (e.g., via `SO_REUSEPORT` multi-socket listeners created per routine). No CA-signed certificate or privileged position is required — the attacker only needs to capture and resend a single legitimate ciphertext from any point on the network path while the tunnel is active, which is the same threat model as ordinary replay attacks this window is meant to defend against.

### Recommendation
Hold a single lock (or otherwise make the operation atomic) across `window.Check`, the decrypt/verify operation's counter-consumption decision, and `window.Update`, so that no two goroutines can simultaneously observe a counter as "not yet seen." Concretely, merge `Check`+`Update` into a single locked "reserve-and-check" step performed before decryption, so a failed decrypt can roll back the reservation but a concurrent duplicate cannot slip through the gap between the two locked sections in `ConnectionState.Decrypt` and `ConnectionState.VerifyRelay`.

### Proof of Concept
1. Establish a tunnel between two nodes with `routines` set to 2+ (or run with `SO_REUSEPORT`-style multiple listener sockets so duplicate packets can land on different goroutines).
2. Capture one legitimate encrypted data-plane (or relay) packet in flight.
3. Simultaneously re-inject two copies of the exact same captured packet targeting the same receiving process (e.g., via two near-simultaneous sends so they race onto different reader goroutines).
4. Observe (following the same technique as `TestRelayReplayProtection`, but racing instead of serializing the duplicate delivery) that both copies pass `window.Check` before either completes `window.Update`, causing the packet to be decrypted and delivered to the tun device twice instead of once — an analog to the linked report's "action proceeds twice because the state update trails the check."

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

**File:** interface.go (L273-288)
```go
func (f *Interface) run() {
	// Launch n queues to read packets from udp
	for i := 0; i < f.routines; i++ {
		f.wg.Go(func() {
			f.listenOut(i)
		})
	}

	// Launch n queues to read packets from tun dev
	for i := 0; i < f.routines; i++ {
		f.wg.Go(func() {
			f.listenIn(f.readers[i], i)
		})
	}

}
```

**File:** interface.go (L309-326)
```go
func (f *Interface) listenOut(i int) {
	var li udp.Conn
	if i > 0 {
		li = f.writers[i]
	} else {
		li = f.outside
	}

	ctCache := firewall.NewConntrackCacheTicker(f.ctx, f.l, f.conntrackCacheTimeout)
	lhh := f.lightHouse.NewRequestHandler()
	plaintext := make([]byte, udp.MTU)
	h := &header.H{}
	fwPacket := &firewall.Packet{}
	nb := make([]byte, 12, 12)

	err := li.ListenOut(func(fromUdpAddr netip.AddrPort, payload []byte) {
		f.readOutsidePackets(ViaSender{UdpAddr: fromUdpAddr}, plaintext[:0], payload, h, fwPacket, lhh, nb, i, ctCache.Get())
	})
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

**File:** CHANGELOG.md (L79-79)
```markdown
- Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them. (#1751)
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
