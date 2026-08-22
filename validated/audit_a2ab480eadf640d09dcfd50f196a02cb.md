I have enough evidence to identify the strongest analog: the check-then-act gap in `ConnectionState.Decrypt` / `VerifyRelay` in `connection_state.go`, confirmed reachable via multiple UDP reader goroutines (`f.routines`, `listenOut`, `SupportsMultipleReaders`) processing packets for the same tunnel concurrently.

### Title
Non-Atomic Replay-Window Check-Then-Update Allows Duplicate Packet Processing via Concurrent Reader Race - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` in `connection_state.go` validate an incoming message counter against the anti-replay sliding window (`Bits.Check`), release the lock, perform the (comparatively expensive) AEAD decryption, and only afterward re-acquire the lock to mark the counter as seen (`Bits.Update`). Because the "check" and "mark-as-seen" operations are split across two separate lock acquisitions instead of being atomic, two packets carrying the identical `messageCounter` that arrive on different reader goroutines can both pass `Check` before either one reaches `Update`. This is structurally the same bug class as the reported Nebula-staking reentrancy issue: a security-critical piece of state (there: `staker.lastUpdatedTimestamp`; here: the anti-replay `window`) is read for a decision, an external/expensive operation is interleaved, and the state is only finalized afterward — creating a window in which the same "already processed" check can be satisfied twice. [1](#0-0) [2](#0-1) 

### Finding Description
Nebula supports multiple concurrent reader routines processing the underlay UDP socket, configured via `routines` and dispatched in `Interface.run()`/`listenOut`, when the platform's UDP implementation supports multiple readers (`SupportsMultipleReaders`). [3](#0-2) [4](#0-3) [5](#0-4) 

Each of these goroutines independently calls `readOutsidePackets`, which for `header.Message` traffic eventually reaches `ConnectionState.Decrypt` (or `VerifyRelay` for relay frames) on the *same* `HostInfo`/`ConnectionState` object — there is no per-connection serialization preventing two packets destined for the same tunnel from being decrypted concurrently on different reader goroutines. [6](#0-5) 

Inside `Decrypt`, the sequence is:
1. Lock, `cs.window.Check(l, messageCounter)`, unlock.
2. `cs.dKey.DecryptDanger(...)` — performed *without* holding `decryptLock`.
3. Lock, `cs.window.Update(l, messageCounter)`, unlock. [1](#0-0) 

An on-path or replaying attacker who can capture and resend a single legitimate ciphertext packet (no valid certificate or credential is needed — this is a pure UDP replay, exactly the scenario the existing `TestRelayReplayProtection` test guards against for relay frames) can inject two or more copies of the same captured packet in quick succession. If they land on different reader routines (`routines > 1`), both can pass step 1's `Check` before either completes step 3's `Update`, because the lock is dropped in between. The result: the same `messageCounter` is decrypted and accepted as valid application data more than once, defeating the anti-replay guarantee that `Bits`/`ReplayWindow` exists to provide. [7](#0-6) 

The `VerifyRelay` path used for relay-forwarded frames has the identical structure and is exercised by `TestRelayReplayProtection`, which explicitly documents that a prior bug allowed replay because the window was never advanced — showing this exact check/update separation is a known-fragile area of the code. [8](#0-7) 

### Impact Explanation
Successful exploitation lets an attacker cause a receiver to accept and process a duplicated encrypted packet as if it were new — i.e., a concrete replay of application traffic across the tunnel. Depending on the payload this can mean duplicate delivery of tunneled packets to the inside `tun` device (potential double-processing of stateful application protocols) or duplicate forwarding through a relay node, undermining the nonce/replay protection that the Noise-based transport keys rely on for message freshness guarantees.

### Likelihood Explanation
The race requires: (a) `routines > 1` (a supported, documented configuration on multi-reader platforms such as Linux with `SO_REUSEPORT`), and (b) the attacker being able to deliver two copies of a captured ciphertext packet closely enough in time that they land on different reader goroutines before the first `Update` completes. Both conditions are realistic for a network-positioned attacker capable of packet capture/replay (no signed certificate needed), making this a plausible, narrow-timing-window race rather than a purely theoretical one.

### Recommendation
Make the check-decrypt-mark sequence atomic with respect to a given `messageCounter`/connection: hold `decryptLock` across the entire `Check` → `Decrypt` → `Update` sequence (accepting the cost of serializing decryption per-connection), or perform an atomic "check-and-reserve" operation on the window before decrypting (marking the slot provisionally) and only reverting it if decryption subsequently fails, so no two goroutines can ever get past the check step for the same counter simultaneously.

### Proof of Concept
1. Configure a Nebula node with `routines` > 1 on Linux (enables `SO_REUSEPORT`/multiple UDP readers, per `SupportsMultipleReaders`).
2. Establish a tunnel and capture one legitimate `header.Message` UDP packet sent by the peer (as done in `TestRelayReplayProtection`'s `relayFrame` capture pattern, adapted to the direct-tunnel `Decrypt` path instead of `VerifyRelay`).
3. Rapidly re-inject (e.g., via two sockets bound with `SO_REUSEPORT` sending simultaneously, or a tight loop) two copies of the exact same captured packet toward the victim node.
4. If timing lands the two copies on different reader goroutines, both may pass `cs.window.Check` before either reaches `cs.window.Update`, resulting in the payload being decrypted and delivered to the tun device (or forwarded, for the relay case) twice — observable as a duplicate packet on the receiving end, analogous to the duplicate-forward outcome the existing `TestRelayReplayProtection` test was written to prevent.

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

**File:** udp/udp_linux.go (L70-72)
```go
func (u *StdConn) SupportsMultipleReaders() bool {
	return true
}
```

**File:** outside.go (L25-87)
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

	case header.RecvError:
		f.handleRecvError(via.UdpAddr, h)
		return
	}

	// Relay packets are special
	isMessageRelay := (h.Type == header.Message && h.Subtype == header.MessageRelay)
```

**File:** bits.go (L14-26)
```go

// Bits is a sliding-window anti-replay tracker. The window is stored as a
// circular bitmap packed into uint64 words (8x denser than a []bool), so a
// length-N window costs N/8 bytes. length must be a power of two.
type Bits struct {
	length             uint64
	lengthMask         uint64
	current            uint64
	bits               []uint64
	lostCounter        metrics.Counter
	dupeCounter        metrics.Counter
	outOfWindowCounter metrics.Counter
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
