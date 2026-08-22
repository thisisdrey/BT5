### Title
Replay-window check/commit split in `ConnectionState.Decrypt`/`VerifyRelay` allows a concurrently-delivered duplicate to bypass anti-replay protection - (File: connection_state.go)

### Summary
This is a direct structural analog of the `MagicSpend` finding: a guard is evaluated ("validation") separately from the state mutation that actually commits the effect of that guard ("execution"), and the lock/critical section is dropped between the two. When the same logical operation can be triggered twice concurrently before the commit happens, the second occurrence still observes the pre-commit state and is wrongly treated as valid — exactly like two `UserOperation`s each independently passing the balance check before either debit is applied.

### Finding Description
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` implement anti-replay via a sliding bitmap window (`Bits`, in `bits.go`). Both functions split the operation into three separate critical sections: [1](#0-0) 

1. `cs.decryptLock.Lock(); result := cs.window.Check(...); cs.decryptLock.Unlock()` — the "validation" step, checking whether `messageCounter` is still unseen.
2. `cs.dKey.DecryptDanger(...)` — performed **without holding `decryptLock`**, i.e. outside the mutex entirely.
3. `cs.decryptLock.Lock(); result = cs.window.Update(...); cs.decryptLock.Unlock()` — the "commit" step that actually marks the counter as seen.

The same pattern exists in `VerifyRelay`: [2](#0-1) 

Between step 1 and step 3, the lock is released. Nebula supports multiple concurrent reader routines processing inbound UDP packets in parallel (`routines`/multiqueue support), each independently invoking `readOutsidePackets` → `ConnectionState.Decrypt`/`VerifyRelay` for packets belonging to the very same tunnel/`ConnectionState`: [3](#0-2) [4](#0-3) 

If an attacker (or an on-path duplicator/NAT re-transmit) delivers two copies of the exact same authenticated packet (same `messageCounter`) in close succession, they can land in two different reader routines. Both goroutines call `Check(messageCounter)` before either has called `Update(messageCounter)`; `Bits.Check` only inspects existing bitmap state and does not reserve the slot atomically with the check: [5](#0-4) 

Both goroutines therefore see `Check == true`, both proceed to independently run `DecryptDanger` on the same ciphertext/counter (this succeeds twice since it's a stateless AEAD verify), and both eventually call `Update`. The second `Update` call will correctly fail (returning `false`/`ErrAlreadySeen`) because `Update` does perform the atomic bitmap write — but by that point the duplicate packet has already been fully authenticated and decrypted, and its plaintext has already been handed off for further processing (e.g., queued to the TUN device or routed) before the "already seen" error is discovered. This mirrors the `MagicSpend` root cause precisely: the check (`Check`) that is supposed to guarantee downstream correctness is evaluated separately from, and without holding a lock through, the point where its guarantee is actually enforced (`Update`), so a second concurrent operation can slip through the same gap that the guard was meant to close.

The CHANGELOG entry "Lock replay window updates so concurrent readers can't corrupt it. (#1802)" indicates the internal `Bits` structure itself was hardened against concurrent corruption of the bitmap words, but the check-then-decrypt-then-update sequencing at the `ConnectionState` level still drops the lock between `Check` and `Update`, leaving the TOCTOU window open at the higher level.

### Impact Explanation
This breaks the anti-replay guarantee of the Noise-based tunnel protocol for any deployment using more than one reader routine (`routines > 1`, which requires multiqueue support on both the tun device and the UDP socket — a supported and documented configuration, not a test-only path). A replayed/duplicated data-plane or relay packet can be accepted and forwarded twice instead of being deduplicated, which is a concrete violation of the anti-replay/traffic-integrity property the window exists to enforce (analogous to double-processing a withdrawal that should have been blocked by a validation check).

### Likelihood Explanation
Requires: (1) `routines > 1` configured, which is a supported multi-queue mode; and (2) an attacker or network condition able to deliver a byte-identical copy of an already-observed ciphertext packet within the narrow window between another routine's `Check` and `Update` calls (network duplication, on-path replay, or an attacker capturing and immediately re-injecting a packet). This is a narrow timing window per packet, but it is systematically reachable — no valid certificate or already-authenticated peer status is required beyond what is already needed to capture/replay a packet on the wire, and firewall/CA checks are irrelevant to this specific bypass.

### Recommendation
Hold `decryptLock` across the entire check-decrypt-update sequence (or, equivalently, make `Check`+reservation atomic, e.g. add a `CheckAndReserve` that provisionally marks the counter before decrypting and rolls back on decrypt failure) so that no second caller can observe `Check == true` for a counter that is already in flight. This closes the gap in the same way the `MagicSpend` mitigation direction ("track in-flight state atomically with the check") was recommended, adapted to hold the lock for the full validate+commit sequence rather than releasing it mid-operation.

### Proof of Concept
1. Configure a node with `routines: 2` (requires multiqueue-capable tun and UDP socket, e.g. Linux).
2. Establish a tunnel and capture one legitimate encrypted `Message` packet (any `messageCounter` N) sent by the peer.
3. Immediately re-inject two copies of the identical captured packet from two sockets/threads/processes in rapid succession, aimed to be picked up by two different reader routines of the target's UDP listener (feasible by flooding, since the kernel distributes reads across `SO_REUSEPORT`/multi-queue sockets somewhat unpredictably).
4. Observe (e.g., via TUN device output, relay-forwarding counters as in `TestRelayReplayProtection` in `e2e/tunnels_test.go`) that both copies of the duplicate packet are decrypted and forwarded/delivered, rather than the second one being rejected before decryption/delivery — because `Check` for both occurred before either `Update` committed the counter to the bitmap.

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

**File:** outside.go (L89-120)
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
