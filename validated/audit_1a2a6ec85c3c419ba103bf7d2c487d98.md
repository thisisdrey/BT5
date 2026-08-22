This confirms the analog: `f.routines` spawns multiple concurrent `listenOut` goroutines (one per UDP reader queue, when `listen.batch`/multiqueue is supported), each of which independently calls `readOutsidePackets` → `ConnectionState.Decrypt`/`VerifyRelay` for packets that may target the very same `hostinfo`/`ConnectionState`. The anti-replay check-then-act sequence in `Decrypt`/`VerifyRelay` is not atomic, which mirrors the report's "check-then-act without persisting the flag" bug class. [1](#0-0) [2](#0-1) [3](#0-2) 

### Title
Time-of-check/time-of-use race in replay-window enforcement allows duplicate processing of a single wire packet - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` implement anti-replay protection by taking `decryptLock`, calling `window.Check`, releasing the lock, performing AEAD decryption unlocked, then re-taking the lock to call `window.Update` and mark the counter seen. Because the "check" and the "mark as seen" (the durable state flag, analogous to a withdrawn/settled flag) are not part of one atomic critical section, two goroutines that race on the same `messageCounter` can both pass `Check` before either calls `Update`, letting the very same wire packet be authenticated and delivered to the data-plane handler more than once. Nebula's multi-queue UDP listener design (`Interface.run` spawning `f.routines` independent `listenOut` goroutines, each calling `readOutsidePackets`) makes concurrent delivery of duplicate/racing packets for the same `hostinfo` a real, reachable condition rather than a theoretical one.

### Finding Description
`Decrypt` performs three discrete, separately-locked steps:
1. Lock, `window.Check(l, messageCounter)`, unlock.
2. AEAD decrypt (no lock held).
3. Lock, `window.Update(l, messageCounter)`, unlock — this is the step that actually records the counter as "seen" in the sliding-window bitmap (`Bits`). [1](#0-0) 

`VerifyRelay` follows the identical pattern for relay-forwarded frames: [4](#0-3) 

The persistent "already processed" flag (the `Bits` window bit for that counter) is only set in step 3, exactly analogous to the Marketplace contract never setting a "withdrawn" flag before allowing the transfer. If a duplicate ciphertext for the same `messageCounter` arrives on two different reader queues at nearly the same time (e.g., due to genuine UDP retransmission on the wire, or an attacker who can inject duplicate underlay UDP datagrams — which requires no CA-signed certificate since these are ordinary UDP packets addressed to the listener), both goroutines can complete `Check` (both return `true`, since neither has advanced `window.current`/set the bit yet) before either reaches `Update`. Both then independently perform a successful AEAD decrypt and hand the plaintext to `handleOutsideMessagePacket`/`handleOutsideRelayPacket`, i.e., the packet is processed twice. This directly parallels the reported bug class: a check for "has this action already happened" that is not atomically coupled with the state mutation that would prevent repetition, permitting the guarded action (secure delivery/relay-forwarding of one packet) to occur more than once for a single instance of the resource.

The multi-reader architecture makes this reachable: `Interface.run` launches `f.routines` independent goroutines each running `listenOut(i)`, and each calls `f.readOutsidePackets` → `ConnectionState.Decrypt`/`VerifyRelay` concurrently for whatever underlay UDP source addresses land on that particular socket/queue. [2](#0-1) [3](#0-2) 

### Impact Explanation
On the data path (`Decrypt`), duplicate processing means a single genuine (or attacker-replayed) encrypted packet from an already-authenticated peer can be delivered to the tun device / firewall / lighthouse handler more than once, defeating the anti-replay guarantee the `Bits` window is meant to provide. On the relay path (`VerifyRelay`), the impact is stronger: `handleOutsideRelayPacket` re-forwards the authenticated relay frame toward the relay target, so a race here causes the relay node to forward the same relayed frame twice — the exact behavior the codebase's own regression test (`TestRelayReplayProtection`) was written to prevent for the sequential-replay case, but the check/update split still leaves a race window open for concurrent delivery. Repeated delivery of application traffic (e.g., non-idempotent commands, TCP segments causing protocol confusion) undermines the confidentiality/integrity guarantee that each ciphertext+nonce pair is processed exactly once.

### Likelihood Explanation
Triggering the race requires two copies of a packet bearing the same `messageCounter` to be in flight to the interface at nearly the same time and to be picked up by two different `routines` queues (or, on relay nodes, the relay's forwarding path racing against itself). This can occur incidentally on lossy/duplicating underlay networks (UDP duplication is a normal, if rare, network condition) and can be deliberately amplified by an unauthenticated attacker who is able to send duplicate UDP datagrams to the listener's socket without needing to hold a valid Nebula certificate, since the outer UDP layer performs no anti-spoofing/anti-duplication itself. `f.routines > 1` is a normal, documented multi-queue configuration (`tun.routines`/`listen.batch`), not an edge case.

### Recommendation
Make the check-and-mark-as-seen operation atomic: hold `decryptLock` across the full `Check` → decrypt → `Update` sequence (or perform an atomic "check-and-reserve" step, e.g. an eager provisional `Update`/reservation on the bit before decrypting, rolling back on decryption failure) so that no other goroutine can observe the same `messageCounter` as still-unclaimed while a decrypt for it is in flight. Apply the same fix symmetrically to `VerifyRelay`.

### Proof of Concept
1. Establish a tunnel between two Nebula nodes with `listen.routines`/`tun.routines` > 1 (or, for the relay case, set up a relay node per `TestRelayReplayProtection`'s topology).
2. Capture one legitimate encrypted data packet (or relay frame) with message counter N.
3. Inject the identical UDP datagram twice in extremely close succession (e.g., via two sockets/threads sending simultaneously, or by using OS-level packet duplication) such that both copies are likely to be picked up by different `listenOut` reader goroutines before the first `Update` call commits.
4. Observe (e.g., via added instrumentation/counters or by watching the tun device / relay's outbound queue) that the plaintext is delivered/forwarded twice instead of the second copy being rejected with `ErrAlreadySeen`, demonstrating the anti-replay window's check-then-act gap.

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
