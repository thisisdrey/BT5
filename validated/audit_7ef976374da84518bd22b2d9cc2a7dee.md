### Title
Anti-replay window bypass via check-then-update race in `ConnectionState.Decrypt`/`VerifyRelay` — ([File: connection_state.go])

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` validate an inbound message counter with `window.Check()`, release the lock, perform the AEAD decrypt, then re-acquire the lock and call `window.Update()` to actually mark the counter as consumed. Because the "check" and the "reserve/commit" steps are not atomic (the lock is dropped in between), two packets carrying the same message counter processed concurrently can both pass `Check()` before either has called `Update()`, exactly mirroring the vault bug class where multiple fulfillment paths could each pass a liquidity check independently because nothing was "reserved" by the earlier path.

### Finding Description
`Decrypt` does:
1. Lock, `window.Check(counter)`, unlock.
2. Decrypt (outside the lock).
3. Lock, `window.Update(counter)`, unlock. [1](#0-0) 

`VerifyRelay` follows the identical pattern for relay frames. [2](#0-1) 

The replay window itself (`Bits.Check`/`Bits.Update`) is only safe if callers serialize the check-then-commit sequence as a single atomic operation; `Check` is a pure read that doesn't mark anything, and only `Update` mutates state and rejects duplicates. [3](#0-2) 

Nebula's `Interface` runs multiple concurrent reader routines (`f.routines`) each calling `readOutsidePackets` independently, and inbound packets are dispatched to a shared `HostInfo`/`ConnectionState` looked up via `f.hostMap.QueryIndex(h.RemoteIndex)`, so packets for the same tunnel can be processed on different goroutines simultaneously. [4](#0-3) [5](#0-4) 

Because `decryptLock` is released between the `Check` and `Update` calls, an attacker who replays (retransmits) a previously observed ciphertext packet for the same tunnel in quick succession can cause two goroutines to both pass `window.Check()` for the same counter before either calls `window.Update()`. Both goroutines then proceed to decrypt and deliver the packet, defeating the intended one-time-use guarantee of the message counter — the analog of the vault's "multiple fulfillments independently pass the liquidity check because reservation happens too late/elsewhere."

### Impact Explanation
A successful race allows a captured/duplicated ciphertext packet to be decrypted and delivered twice (or more) to the tun interface, which is precisely what the sliding-window anti-replay mechanism exists to prevent. This is a concrete replay-acceptance bypass: duplicate application data can be re-injected into the tunnel, which can be leveraged for traffic duplication attacks, disrupting state depending on the higher-level protocol carried inside the tunnel, or amplifying a captured packet's effect. This satisfies the "traffic decryption/forgery/replay" impact category.

### Likelihood Explanation
Exploitation requires only the ability to send (or resend) a previously-observed encrypted UDP datagram to the target twice within a very small time window and requires `f.routines > 1` (multi-queue mode, common on Linux with multiqueue TUN support) so the two copies are picked up by different goroutines and racing the same `ConnectionState`. No CA-signed certificate or handshake participation is required — the attacker only needs network-level ability to duplicate/replay an already-captured ciphertext packet, matching the required "no CA-signed certificate" reachability constraint. The race window is narrow (bounded by one AEAD decrypt operation) but is not artificially or structurally prevented; under load or with intentionally duplicated packets, the probability of hitting the window increases.

### Recommendation
Make the anti-replay check-and-commit atomic with respect to a given counter: either hold `decryptLock` across the full check → decrypt → update sequence (accepting the crypto cost inside the lock), or perform an atomic "reserve" step (equivalent to `Update`) before decrypting and roll it back only on genuine decrypt failure, rather than checking early and finalizing late. This mirrors the recommended vault fix of making a single call the sole source of truth for the "reservation" (window-update) instead of splitting the check and the commit across a released lock.

### Proof of Concept
Conceptually:
1. Establish a tunnel; capture one valid encrypted data-plane packet with counter `N`.
2. Send two copies of that exact packet to the victim's UDP listener nearly simultaneously so they land on two different `listenOut(i)` goroutines (routines > 1).
3. Both goroutines call `cs.window.Check(l, N)` before either calls `cs.window.Update(l, N)` (window shows `N` as not-yet-seen for both, since `Check` alone doesn't mutate state) — see the lock-release gap in: [1](#0-0) 
4. Both goroutines successfully decrypt and forward the plaintext to `f.readers[q].Write(out)`, resulting in duplicate delivery of a single wire packet despite the anti-replay window being present.

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

**File:** bits.go (L135-150)
```go
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

**File:** outside.go (L89-121)
```go
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
