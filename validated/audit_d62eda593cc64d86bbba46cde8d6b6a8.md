### Title
Anti-replay bypass via Check/Update TOCTOU race in `ConnectionState.Decrypt` allows a duplicated/replayed data packet to be accepted twice - (File: connection_state.go)

### Summary
`nebula`'s data-plane anti-replay protection is implemented by a check-then-decrypt-then-update pattern that releases the guarding mutex between the "check" and "update" steps, mirroring the class of bug in the referenced report where an accumulator/claim state is read and later committed as two separate, non-atomic operations, letting a second actor race in before the first commit lands and "claim" the same slot.

### Finding Description
`ConnectionState.Decrypt` first acquires `decryptLock`, calls `cs.window.Check(l, messageCounter)` to test whether the counter has already been seen, then **releases the lock**, performs AEAD decryption (`cs.dKey.DecryptDanger`), and only afterward re-acquires the lock to call `cs.window.Update(l, messageCounter)`, which is what actually marks the counter as seen: [1](#0-0) 

Because the lock is dropped for the entire duration of decryption, two goroutines processing the same `messageCounter` concurrently (e.g. an attacker sending the same UDP datagram twice, or exploiting network duplication/retransmission) can both execute `Check` while the bit is still unset, both pass, both decrypt successfully, and both return the plaintext to the caller as a "new" accepted packet. The second `Update` call still returns `false` (correctly reporting a duplicate), but by then the decrypted payload has already been handed to the packet-processing pipeline once via the first `Decrypt` call and a second time via the racing call — the anti-replay window (`Bits`, seeded and updated via `NewBits`/`Update`/`Check` in `bits.go`) is only consulted for admission, not enforced atomically with the actual decrypt+accept.

This is directly analogous to the referenced Stakehouse bug, where `claimed`/`accumulatedETHPerLPShare` were read and later written in a way that let two claim operations both see the "pre-commit" state and successfully both extract value intended to be claimable only once. Here, the "claim" is the single-use replay-window slot for a given `messageCounter`, and the "check" (`Bits.Check`) and "commit" (`Bits.Update`) are non-atomic with respect to the expensive operation (decryption) sandwiched between them.

`VerifyRelay` exhibits the identical pattern for relay-frame authentication: [2](#0-1) 

The interface is architected to process inbound UDP packets across multiple concurrent reader routines (`f.routines`), each independently invoking the receive path that eventually calls into `ConnectionState.Decrypt` for the same tunnel/`ConnectionState`, making the race reachable without any special local privileges — only network-level packet duplication/replay by a remote peer: [3](#0-2) [4](#0-3) 

### Impact Explanation
A successful race allows a single captured/duplicated encrypted data-plane (or relay) packet to be decrypted and delivered to the tun device (or relay signature verification path) more than once, defeating the intended anti-replay guarantee that `ReplayWindow`/`Bits` is meant to provide. Depending on the payload, this can result in remote-triggered duplicate application-layer traffic delivery (a concrete traffic-replay bypass), which is the exact impact class called out as acceptable in the validation rules (traffic decryption/forgery/replay).

### Likelihood Explanation
Exploitability requires only the ability to deliver (or cause delivery of, e.g. via network-level duplication or a malicious intermediate router/NAT) the same ciphertext packet to the target twice in close succession while multiple reader routines (`routines > 1`, common on multi-core Linux hosts using multiqueue) are active, so the two `Decrypt` calls can interleave their lock-released decrypt phases. No valid certificate or established trust beyond the already-completed handshake is required; the race is purely a data-plane concurrency issue independent of peer identity.

### Recommendation
Hold `decryptLock` across the entire `Check` → `Decrypt` → `Update` sequence (or perform an atomic "test-and-set" reservation of the counter bit before decrypting, rolling back on decrypt failure) so that no other goroutine can observe an unset bit for a counter that is currently being processed. Apply the same fix to `VerifyRelay`.

### Proof of Concept
Conceptual PoC (requires concurrency harness, not verified end-to-end due to index limitations):
1. Establish a tunnel with `routines > 1` (multi-reader UDP listener) so `listenOut` runs on multiple goroutines feeding the same `Interface`/`ConnectionState`.
2. Capture one legitimate data-plane packet with a known `messageCounter` N.
3. Send two copies of the identical UDP datagram to the target in immediate succession so they land on two different reader goroutines.
4. Both goroutines call `ConnectionState.Decrypt`; both call `window.Check(l, N)` before either calls `window.Update(l, N)`, since the lock is released during the AEAD decrypt call. Both `Check` calls return `true`, both decrypts succeed, and both plaintexts are delivered onward — the packet is processed twice despite the replay window being intended to block the duplicate. [1](#0-0)

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

**File:** interface.go (L309-327)
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
