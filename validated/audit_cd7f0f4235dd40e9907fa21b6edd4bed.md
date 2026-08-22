## Analog Found

### Title
Replay-window TOCTOU in `ConnectionState.Decrypt`/`VerifyRelay` allows anti-replay bypass under concurrent packet processing - (File: connection_state.go)

### Summary
The external report describes a reentrancy flaw where state (balances/authorization) is checked, then an external call is made, and only afterward is state updated — leaving a window where the check can be satisfied twice for what should be a one-time action. The reachable analog in this codebase is the anti-replay window check-then-update pattern in `ConnectionState.Decrypt` and `ConnectionState.VerifyRelay`, where the "external call" is the AEAD decrypt operation performed *outside* the lock that protects the replay window, between the `Check` and `Update` steps.

### Finding Description
`ConnectionState.Decrypt` releases `decryptLock` after `window.Check` succeeds, performs `DecryptDanger` (the potentially slow/blocking cryptographic operation) without holding the lock, and only re-acquires the lock afterward to call `window.Update`: [1](#0-0) 

The same check-decrypt-then-update pattern, with the same drop of the lock across the decrypt step, exists in `VerifyRelay`: [2](#0-1) 

This is invoked from the UDP inbound packet path in `readOutsidePackets`, which is dispatched per-queue by `listenOut`: [3](#0-2) [4](#0-3) 

When `listen.routines` is configured greater than 1, `Interface.run` starts multiple `listenOut` goroutines, each with its own UDP socket, that can independently call `readOutsidePackets` → `ConnectionState.Decrypt` for packets belonging to the same tunnel/`HostInfo` concurrently: [5](#0-4) [6](#0-5) 

Because `window.Check` and `window.Update` are two separate, independently-locked operations with the AEAD decrypt sandwiched in between while unlocked, two concurrent invocations of `Decrypt` (or `VerifyRelay`) carrying the **same** `messageCounter` can both pass `Check` before either calls `Update`. Both would then independently succeed at decrypting the same ciphertext and both would be accepted as valid, non-replayed packets — analogous to the reported reentrancy bug where state (the "already spent" marker) is not committed atomically around the externally-observable operation.

### Impact Explanation
This defeats the anti-replay protection that the sliding window (`cs.window`, `ReplayWindow = 1024`) is designed to enforce. An attacker who captures a single valid encrypted Nebula data/message packet from a legitimate sender (e.g., via a compromised link, ARP spoofing on a LAN segment, or any other on-path vantage point that does not require holding a CA-signed certificate) can duplicate and replay it; if the duplicate lands on a different queue/goroutine than the original at nearly the same time, both copies can be accepted and delivered to the firewall/TUN device rather than one being rejected as `ErrAlreadySeen`. This is a concrete traffic-replay weakness in the data plane's authentication/integrity guarantees.

### Likelihood Explanation
Exploitability depends on `listen.routines` > 1 (multi-queue mode) so that packets for the same tunnel can be handled by different goroutines/sockets concurrently, and on the attacker being able to inject a duplicate of a captured ciphertext within the narrow window between `Check` and `Update`. This is a race condition, so it is not reliably triggerable on every attempt, but it is a genuine correctness gap in the replay-window locking discipline rather than a theoretical concern, since the lock is explicitly dropped across the decrypt call.

### Recommendation
Hold `decryptLock` for the entire duration of the check-decrypt-update sequence in both `Decrypt` and `VerifyRelay`, so that the replay-window check and the corresponding update are atomic with respect to the decrypt operation, closing the TOCTOU window: e.g., acquire the lock once at the top of the function and release it only after `window.Update` (or use a compare-and-set style single locked call that both checks and reserves the counter before releasing the lock for the decrypt to run, then confirms/reverts under lock based on the decrypt result).

### Proof of Concept
1. Configure a Nebula node with `listen.routines` set to a value greater than 1 (multi-queue enabled).
2. Establish a tunnel and capture one valid encrypted `header.Message` packet destined for that node.
3. Immediately re-transmit an exact duplicate of the captured packet to the node's listen sockets in a tight loop timed to race across the multiple reader goroutines.
4. Observe (via added logging/metrics around `window.Check`/`window.Update`, or by checking for duplicate delivery to the TUN device / duplicate firewall conntrack effects) that both the original and the replayed duplicate are decrypted and accepted rather than the replay being rejected with `ErrAlreadySeen`.

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

**File:** outside.go (L126-132)
```go
	out, err = hostinfo.ConnectionState.Decrypt(f.l, h.MessageCounter, out, packet, nb)
	if err != nil {
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("Failed to decrypt packet", "error", err, "from", via, "header", h)
		}
		return
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

**File:** interface.go (L309-337)
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

	// An error after teardown began is shutdown noise, the closed flag covers resources
	// Close releases itself and the cancelled ctx covers ones torn down by their owners
	// reacting to it, like the user device pipes
	if err != nil && !f.closed.Load() && f.ctx.Err() == nil {
		f.l.Error("Error while reading inbound packet, closing", "error", err)
		f.onFatal(err)
	}

	f.l.Debug("underlay reader is done", "reader", i)
}
```
