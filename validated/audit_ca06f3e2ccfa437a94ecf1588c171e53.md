### Title
Anti-Replay Window Check-Then-Act Race in `ConnectionState.Decrypt` Allows Packet Replay Across Multi-Queue UDP Readers - (File: connection_state.go)

### Summary
Nebula's replay protection (`Bits` sliding-window anti-replay tracker) is validated via a `Check`-then-`Update` pattern that releases the lock guarding the window state between the "is this counter valid" check and the AEAD decrypt, and only re-acquires the lock afterward to mark the counter as consumed. When Nebula runs with multiple UDP reader routines (`routines` / multi-queue UDP, configured in `InterfaceConfig.routines` and spawned per-queue in `listenOut`), two copies of the same encrypted packet delivered concurrently on different queues for the same tunnel can both pass the `Check` before either calls `Update`, exactly mirroring the cached-value TOCTOU pattern in the external report where a value is read, acted upon, and only written back to the cache afterward — leaving a manipulable window in between.

### Finding Description
`ConnectionState.Decrypt` reads the anti-replay window state, performs an unlocked cryptographic operation, and only then writes the updated state back, with the lock dropped in between: [1](#0-0) 

Specifically:
1. `cs.decryptLock.Lock(); result := cs.window.Check(l, messageCounter); cs.decryptLock.Unlock()` — reads whether `messageCounter` is fresh.
2. `cs.dKey.DecryptDanger(...)` — the actual AEAD decrypt, executed **without holding `decryptLock`**.
3. `cs.decryptLock.Lock(); result = cs.window.Update(l, messageCounter); cs.decryptLock.Unlock()` — only now is the counter marked "seen" in the `Bits` structure.

The `Bits` sliding-window implementation itself is correct and non-idempotent by design — `Check` is a pure read and `Update` is the only method that mutates the bitmap: [2](#0-1) 

Because `Check` and `Update` are two independently-locked, non-atomic operations rather than a single atomic "test-and-set," a captured, duplicated ciphertext with the same `MessageCounter` that is delivered twice in close succession can have both copies pass `Check` before either reaches `Update`, if they are processed by different goroutines concurrently. This is directly analogous to the report's root cause: a value is read to make a security decision (whether to accept), the decision is acted on, and the authoritative state is only "cached"/committed afterward — with a window in between where a second, colliding operation can slip through.

This is reachable by an unauthenticated network attacker (no valid CA-signed certificate is required) because it operates purely on the wire format of already-established encrypted traffic: the attacker only needs to capture and duplicate ciphertext (a passive/on-path capability, or even blind duplication of observed UDP datagrams), not decrypt or forge it. `readOutsidePackets`, which calls `Decrypt`, is invoked per UDP reader goroutine; Nebula explicitly supports multiple reader routines/queues (`InterfaceConfig.routines`, `Interface.routines`, `f.writers[i]` / `f.readers[q]` arrays, and per-queue `listenOut(i)` goroutines each running their own `ConntrackCacheTicker`/`readOutsidePackets` loop): [3](#0-2) [4](#0-3) 

If duplicate copies of a captured Nebula data-plane, control, or `Test`/`CloseTunnel` packet land on two different queues (which is plausible since OS-level UDP queue selection typically hashes on the 4-tuple/flow — but an attacker fully controls injected duplicate packets and their timing/arrival characteristics against a receiver's multiple sockets), the two goroutines' `Decrypt` calls race, and both can succeed against the same `messageCounter`, defeating the anti-replay guarantee for that specific counter.

### Impact Explanation
A successful race allows a single legitimate ciphertext to be accepted and processed twice by the tunnel's data plane despite Nebula's stated per-tunnel anti-replay guarantee. Depending on which message type is duplicated, this can manifest as:
- Replay of application traffic delivered to the local TUN device twice (state poisoning of upper-layer protocols relying on Nebula's replay protection as a security boundary).
- Replay of `header.CloseTunnel`, `header.Control` (relay control), or `header.Test` messages, causing unintended tunnel teardown, relay state changes, or roaming side effects to be re-triggered outside of the sender's intent.
- Undermines the core security property advertised for `ConnectionState.Decrypt`/`VerifyRelay` (`connection_state.go:84-108` has the identical Check/Update split for relay frames), which the project's own `TestRelayReplayProtection` test explicitly guards against for the *missing-Update* case; this report identifies a *race-window* variant of the same class of bug that is not covered by that regression test, since it requires true concurrent delivery rather than a single-threaded resend.

### Likelihood Explanation
Exploitation requires (a) the ability to capture/duplicate an in-flight ciphertext (standard replay-attack precondition, achievable by any network-adjacent or on-path attacker, no CA cert needed) and (b) multi-queue/multi-routine UDP delivery causing the duplicate to be processed on a different goroutine than the original before `Update` commits. Likelihood is non-trivial but not guaranteed on every attempt — it is a genuine race window bound by the time between `window.Check` and `window.Update` (which includes a full AEAD decrypt), and by the deployment's `routines`/queue configuration. This mirrors the "sandwich" precondition in the source report: the attacker doesn't need certainty on the first try, only a timing window that can be repeated until it lands.

### Recommendation
Hold `decryptLock` for the entire duration of the check-decrypt-update sequence (or otherwise make `Check`+`Update` atomic with respect to the decrypt operation) so that no second goroutine can observe a "not yet seen" state for a `messageCounter` that is concurrently being validated. Alternatively, collapse `Check`+`Update` into a single atomic "reserve-then-decrypt-or-rollback" operation, and apply the same fix to `VerifyRelay`, which has the identical split.

### Proof of Concept
1. Establish a tunnel between two Nebula nodes with `listen.routines` (or the multi-queue UDP configuration) set to more than 1, so multiple `listenOut` goroutines process inbound UDP packets for the same `Interface`/hostinfo concurrently.
2. Capture a single legitimate encrypted data-plane UDP datagram sent to the receiving node (e.g., via a raw socket, on-path tap, or NAT device).
3. Immediately re-inject two copies of the exact same captured datagram at the receiver, timed so they are likely to be picked up by two different reader queues/goroutines (e.g., by sending them back-to-back at high rate, or exploiting known kernel queue-selection hashing/timing behavior).
4. Instrument or observe `ConnectionState.Decrypt` (`connection_state.go:61-82`): under the race, both goroutines' `cs.window.Check(l, messageCounter)` calls can return `true` because neither has yet called `cs.window.Update`, causing both `DecryptDanger` calls to succeed and the packet to be delivered twice to `f.readers[q]`/the TUN device (or to trigger `CloseTunnel`/`Control` handling twice), which is the observable proof that the anti-replay window was bypassed for that counter.

Note: I was unable to fully verify, from the indexed portion of the codebase, whether the OS-level UDP multi-queue delivery in this deployment guarantees per-flow goroutine affinity strongly enough to make the race trivially reproducible versus merely theoretically possible; a live/dynamic test (e.g., a Devin session with terminal access) would be needed to empirically confirm the race window's practical exploitability and quantify success rate under realistic `routines` counts.

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

**File:** interface.go (L40-94)
```go
	DropMulticast      bool
	routines           int
	MessageMetrics     *MessageMetrics
	version            string
	relayManager       *relayManager
	punchy             *Punchy

	tryPromoteEvery uint32
	reQueryEvery    uint32
	reQueryWait     time.Duration

	ConntrackCacheTimeout time.Duration
	l                     *slog.Logger
}

type Interface struct {
	hostMap               *HostMap
	outside               udp.Conn
	inside                overlay.Device
	pki                   *PKI
	firewall              *Firewall
	connectionManager     *connectionManager
	handshakeManager      *HandshakeManager
	dnsServer             *dnsServer
	createTime            time.Time
	lightHouse            *LightHouse
	myBroadcastAddrsTable *bart.Lite
	myVpnAddrs            []netip.Addr // A list of addresses assigned to us via our certificate
	myVpnAddrsTable       *bart.Lite
	myVpnNetworks         []netip.Prefix // A list of networks assigned to us via our certificate
	myVpnNetworksTable    *bart.Lite
	dropLocalBroadcast    bool
	dropMulticast         bool
	routines              int
	disconnectInvalid     atomic.Bool
	closed                atomic.Bool
	relayManager          *relayManager

	tryPromoteEvery atomic.Uint32
	reQueryEvery    atomic.Uint32
	reQueryWait     atomic.Int64

	sendRecvErrorConfig   recvErrorConfig
	acceptRecvErrorConfig recvErrorConfig

	// rebindCount is used to decide if an active tunnel should trigger a punch notification through a lighthouse
	rebindCount int8
	version     string

	conntrackCacheTimeout time.Duration

	ctx     context.Context
	writers []udp.Conn
	readers []io.ReadWriteCloser
	wg      sync.WaitGroup
```

**File:** outside.go (L25-132)
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
		f.handleOutsideRelayPacket(hostinfo, via, out, packet, h, fwPacket, lhf, nb, q, localCache)
		return
	}

	out, err = hostinfo.ConnectionState.Decrypt(f.l, h.MessageCounter, out, packet, nb)
	if err != nil {
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("Failed to decrypt packet", "error", err, "from", via, "header", h)
		}
		return
	}
```
