### Title
Unbounded relay-triggered outbound handshake state via unauthenticated `CreateRelayRequest` targets - (relay_manager.go)

### Finding Description
`relayManager.handleCreateRelayRequest` is reached from `HandleControlMsg`, which is invoked for any `NebulaControl_CreateRelayRequest` message on an already-authenticated but otherwise non-privileged tunnel `h` [1](#0-0) . When the relay target is not the receiving node itself and the node has `am_relay` enabled, the code looks up `peer := rm.hostmap.QueryVpnAddr(target)`; if no hostinfo exists for that (attacker-supplied) `target`, it unconditionally calls `f.Handshake(target)` and returns [2](#0-1) . `Interface.Handshake` explicitly does **not** validate that `target` is within `myVpnNetworksTable`, unlike the normal inside-packet path (`getOrHandshakeNoRouting`) [3](#0-2) . It forwards straight to `HandshakeManager.GetOrHandshake` → `StartHandshake`, which allocates a new `HandshakeHostInfo`, inserts it into the `hm.vpnIps` map keyed by the attacker-chosen address, arms `OutboundHandshakeTimer`, and issues a `lightHouse.QueryServer(vpnAddr)` for that address [4](#0-3) . There is no per-sender rate limit, no cap on the number of distinct pending vpnIps entries, and no authorization check that the requester is allowed to make the relay node originate handshakes to third-party addresses. Because `target` is attacker-controlled and can be any address (not necessarily an existing mesh node), an attacker holding a single authenticated tunnel to a relay-capable node can enumerate many distinct target addresses to repeatedly grow `hm.vpnIps` and trigger `lightHouse.QueryServer` calls.

### Impact Explanation
This causes state growth (`hm.vpnIps` map entries, `HandshakeHostInfo` allocations) and outbound lighthouse query traffic proportional to the number of distinct attacker-chosen targets, without any bound tied to the requester's identity — a resource-exhaustion / bounded-state-invariant violation on relay-capable nodes. However, the amplification/SSRF concern described in the question does not fully hold: when `target` is a genuinely non-existent address, `hostinfo.remotes` resolves to no usable UDP endpoints (via the trusted lighthouse's own answer, not an attacker-supplied endpoint), so `handleOutbound` sends stage-0 packets only to addresses the local lighthouse/cert infrastructure vouches for, not to arbitrary attacker-chosen third-party UDP destinations. So this is a local resource/state exhaustion issue on the relay node (bounded queue/map growth, extra lighthouse query load, retry timers), not a true UDP reflection/amplification attack against third parties.

### Likelihood Explanation
Requires the attacker to already hold one authenticated tunnel to a node configured with `relay.am_relay = true`, which is a legitimate but somewhat narrower precondition (not every node runs as a relay). Given that precondition, the attack is trivial and fully repeatable — the attacker can enumerate arbitrarily many distinct target addresses inside `CreateRelayRequest` control messages with no throttling.

### Recommendation
In `handleCreateRelayRequest`, before calling `f.Handshake(target)` for an unknown peer: (1) validate that `target` is within a plausible/known VPN network (mirroring the check done in `getOrHandshakeNoRouting`), and (2) apply a per-sender-hostinfo rate limit / cap on the number of distinct relay-triggered handshake attempts (and `hm.vpnIps` entries) that a single remote peer can cause, so an attacker cannot use one relay control channel to unboundedly grow handshake/relay state on the relay node.

### Proof of Concept
Unit/fuzz test plan (to run against `relayManager.handleCreateRelayRequest`):
1. Configure a `relayManager` with `am_relay = true` and a stub `Interface`/`HandshakeManager` exposing `hm.vpnIps` size.
2. From one attacker `HostInfo` `h`, send N `CreateRelayRequest` control messages with N distinct, never-before-seen `RelayToAddr` values via `rm.HandleControlMsg`.
3. Assert that after N requests, `len(hm.vpnIps)` (or an equivalent metric such as `metricInitiated`) grows unboundedly/linearly with N rather than being capped or rate-limited per sender — i.e., there is no RESOURCE/STATE bound enforced today.
4. Compare against expected behavior: the count of concurrently pending handshake attempts attributable to a single relay-request sender should be capped; the test should fail against current code (no cap) and pass once a per-sender limit/validation is added.

### Citations

**File:** relay_manager.go (L298-342)
```go
func (rm *relayManager) HandleControlMsg(h *HostInfo, d []byte, f *Interface) {
	msg := &NebulaControl{}
	err := msg.Unmarshal(d)
	if err != nil {
		h.logger(f.l).Error("Failed to unmarshal control message", "error", err)
		return
	}

	var v cert.Version
	if msg.OldRelayFromAddr > 0 || msg.OldRelayToAddr > 0 {
		v = cert.Version1

		b := [4]byte{}
		binary.BigEndian.PutUint32(b[:], msg.OldRelayFromAddr)
		msg.RelayFromAddr = netAddrToProtoAddr(netip.AddrFrom4(b))

		binary.BigEndian.PutUint32(b[:], msg.OldRelayToAddr)
		msg.RelayToAddr = netAddrToProtoAddr(netip.AddrFrom4(b))
	} else {
		v = cert.Version2
	}

	// validate:
	switch msg.Type {
	case NebulaControl_CreateRelayRequest, NebulaControl_CreateRelayResponse:
		if msg.RelayFromAddr == nil {
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				h.logger(f.l).Debug("Control message received with nil RelayFromAddr", "type", msg.Type)
			}
			return
		} else if msg.RelayToAddr == nil {
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				h.logger(f.l).Debug("Control message received with nil RelayToAddr", "type", msg.Type)
			}
			return
		}
	}

	switch msg.Type {
	case NebulaControl_CreateRelayRequest:
		rm.handleCreateRelayRequest(v, h, f, msg)
	case NebulaControl_CreateRelayResponse:
		rm.handleCreateRelayResponse(v, h, f, msg)
	}
}
```

**File:** relay_manager.go (L525-536)
```go
	} else {
		// the target is not me. Create a relay to the target, from me.
		if !rm.GetAmRelay() {
			return
		}
		peer := rm.hostmap.QueryVpnAddr(target)
		if peer == nil {
			// Try to establish a connection to this host. If we get a future relay request,
			// we'll be ready!
			f.Handshake(target)
			return
		}
```

**File:** inside.go (L128-142)
```go
// Handshake will attempt to initiate a tunnel with the provided vpn address. This is a no-op if the tunnel is already established or being established
// it does not check if it is within our vpn networks!
func (f *Interface) Handshake(vpnAddr netip.Addr) {
	f.handshakeManager.GetOrHandshake(vpnAddr, nil)
}

// getOrHandshakeNoRouting returns nil if the vpnAddr is not routable.
// If the 2nd return var is false then the hostinfo is not ready to be used in a tunnel
func (f *Interface) getOrHandshakeNoRouting(vpnAddr netip.Addr, cacheCallback func(*HandshakeHostInfo)) (*HostInfo, bool) {
	if f.myVpnNetworksTable.Contains(vpnAddr) {
		return f.handshakeManager.GetOrHandshake(vpnAddr, cacheCallback)
	}

	return nil, false
}
```

**File:** handshake_manager.go (L358-411)
```go
// StartHandshake will ensure a handshake is currently being attempted for the provided vpn ip
func (hm *HandshakeManager) StartHandshake(vpnAddr netip.Addr, cacheCb func(*HandshakeHostInfo)) *HostInfo {
	hm.Lock()

	if hh, ok := hm.vpnIps[vpnAddr]; ok {
		// We are already trying to handshake with this vpn ip
		if cacheCb != nil {
			cacheCb(hh)
		}
		hm.Unlock()
		return hh.hostinfo
	}

	hostinfo := &HostInfo{
		vpnAddrs:        []netip.Addr{vpnAddr},
		HandshakePacket: make(map[uint8][]byte, 0),
		relayState: RelayState{
			relays:         nil,
			relayForByAddr: map[netip.Addr]*Relay{},
			relayForByIdx:  map[uint32]*Relay{},
		},
	}

	hh := &HandshakeHostInfo{
		hostinfo:  hostinfo,
		startTime: time.Now(),
	}
	hm.vpnIps[vpnAddr] = hh
	hm.metricInitiated.Inc(1)
	hm.OutboundHandshakeTimer.Add(vpnAddr, hm.config.tryInterval)

	if cacheCb != nil {
		cacheCb(hh)
	}

	// If this is a static host, we don't need to wait for the HostQueryReply
	// We can trigger the handshake right now
	_, doTrigger := hm.lightHouse.GetStaticHostList()[vpnAddr]
	if !doTrigger {
		// Add any calculated remotes, and trigger early handshake if one found
		doTrigger = hm.lightHouse.addCalculatedRemotes(vpnAddr)
	}

	if doTrigger {
		select {
		case hm.trigger <- vpnAddr:
		default:
		}
	}

	hm.Unlock()
	hm.lightHouse.QueryServer(vpnAddr)
	return hostinfo
}
```
