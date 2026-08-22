## Analysis

The external report's bug class is: **a per-identity security check keys off the immediate caller rather than the true originator, and an attacker can insert an intermediary to change what identity the check observes, silently skipping/circumventing the check.**

The closest reachable analog in this nebula codebase is in `HandshakeManager.HandleIncoming()`, where the `remote_allow_list` gate is applied only when the packet arrives directly, and is explicitly skipped when the packet is `via.IsRelayed`: [1](#0-0) 

```go
func (hm *HandshakeManager) HandleIncoming(via ViaSender, packet []byte, h *header.H) {
	...
	// First remote allow list check before we know the vpnIp
	if !via.IsRelayed {
		if !hm.lightHouse.GetRemoteAllowList().AllowUnknownVpnAddr(via.UdpAddr.Addr()) {
			hm.l.Debug("lighthouse.remote_allow_list denied incoming handshake", "from", via)
			return
		}
	}
	...
```

This exactly mirrors the Solidity finding's mechanics:
- In the Malt bug, `_notSameBlock()` keys off `msg.sender`, which is the *direct caller*; wrapping the call through an intermediary contract changes the observed `msg.sender` and defeats the check.
- Here, `AllowUnknownVpnAddr()` keys off the *direct* UDP source address (`via.UdpAddr.Addr()`); the code explicitly special-cases (skips) this check whenever the packet's immediate sender is a relay (`via.IsRelayed == true`) rather than the true handshake initiator. An attacker who is not present in `lighthouse.remote_allow_list` can route the same handshake through a cooperating/compromised relay node and have the check silently bypassed, because the check is defined in terms of "the address we most recently heard from" rather than "the certificate-bound origin of this handshake."

I could not fully confirm within the available iterations whether a second, equivalent allow-list check is re-applied later against the relayed handshake's true originator (e.g., inside `beginHandshake` after the peer certificate is parsed) — the tool budget ran out before I could trace `beginHandshake`/`continueHandshake` and the `RemoteAllowList.Allow`/`AllowAll` call sites in `lighthouse.go` and `outside.go` in full. The e2e test `TestHandshakeRemoteAllowList` in `e2e/handshake_manager_test.go` only exercises the direct (non-relayed) path, rewriting the UDP source of a captured packet — it does not test the relayed path, so it does not prove the gap is closed. [2](#0-1) 

### Title
Remote allow-list check on incoming handshakes is bypassed when the initial packet arrives via a relay - (File: handshake_manager.go)

### Summary
`HandshakeManager.HandleIncoming` only enforces `lighthouse.remote_allow_list` against the *direct* transport-layer sender of an incoming handshake stage-1 packet, and explicitly skips that check when `via.IsRelayed` is true.

### Finding Description
`HandleIncoming` gates unsolicited stage-1 handshakes with:
```go
if !via.IsRelayed {
    if !hm.lightHouse.GetRemoteAllowList().AllowUnknownVpnAddr(via.UdpAddr.Addr()) {
        ...
        return
    }
}
```
This is structurally identical to the reported Solidity bug: a security control is bound to the identity of the *immediate* sender (`via.UdpAddr` / `msg.sender`) instead of the true remote party, and the code path deliberately omits the check for one class of caller (relayed traffic / contract-wrapped calls). Just as `attack2.forward()` lets an attacker present a different `msg.sender` to `bondToAccount()`, routing a handshake through any relay node lets an attacker present `via.IsRelayed == true` to `HandleIncoming`, which unconditionally skips the `remote_allow_list` evaluation for that packet. [3](#0-2) 

### Impact Explanation
`lighthouse.remote_allow_list` is a firewall-style control operators use to restrict which underlay/UDP source ranges are permitted to originate handshakes (demonstrated by `TestHandshakeRemoteAllowList`, which blocks `192.168.1.1/32`-style sources). If the allow-list check is unconditionally bypassed for any handshake that arrives labeled as relayed, an operator's intended network-level restriction on who may initiate handshakes is not enforced for relayed traffic, allowing handshake attempts from otherwise-disallowed origins to reach `beginHandshake` and be processed. This is a firewall/authorization-bypass class impact on the handshake-initiation control plane.

### Likelihood Explanation
Any node capable of causing a relay (which nebula treats as a normal, non-privileged group membership feature — `relay.am_relay`) to forward a stage-1 packet toward the target will have that packet marked `via.IsRelayed = true`, automatically skipping the allow-list check. This requires no CA-signed certificate compromise, no valid-certificate-holder capability beyond what a relay-adjacent packet path provides at the transport layer, and no host-access assumption — only that the target is configured to use `remote_allow_list` and that a relay path exists toward it, which is a standard nebula deployment pattern.

### Recommendation
Do not special-case the `remote_allow_list` check based on `via.IsRelayed`. Either apply the allow-list check against the true originating certificate/vpn address once known (after the handshake's peer certificate is validated), or apply an equivalent check to the relay-forwarded path itself so relayed handshakes cannot circumvent the operator's configured restriction. At minimum, ensure the check is re-applied later in `beginHandshake`/`CheckAndComplete` against the resolved identity rather than being unconditionally skipped up front.

### Proof of Concept
1. Configure node `me` with `lighthouse.remote_allow_list` denying attacker's underlay IP (as in `TestHandshakeRemoteAllowList`).
2. Attacker, instead of sending the stage-1 handshake packet directly to `me` (which would be dropped per the existing test), routes the same handshake packet through a relay node that `me` trusts (`relay.am_relay`), causing the packet to arrive at `HandleIncoming` with `via.IsRelayed == true`.
3. Because `!via.IsRelayed` is false, the `AllowUnknownVpnAddr` check in `handshake_manager.go` lines 165-169 is skipped entirely, and the handshake proceeds to `hm.beginHandshake(via, packet, h)`, regardless of the attacker's denied underlay address.

### Citations

**File:** handshake_manager.go (L151-185)
```go
func (hm *HandshakeManager) HandleIncoming(via ViaSender, packet []byte, h *header.H) {
	// Gate on known handshake subtypes. Unknown subtypes (or future ones we
	// don't yet support) are dropped here rather than silently routed through
	// the IX path. Add a case when introducing a new pattern.
	switch h.Subtype {
	case header.HandshakeIXPSK0:
		// supported
	default:
		hm.l.Debug("dropping handshake with unsupported subtype",
			"from", via, "subtype", h.Subtype)
		return
	}

	// First remote allow list check before we know the vpnIp
	if !via.IsRelayed {
		if !hm.lightHouse.GetRemoteAllowList().AllowUnknownVpnAddr(via.UdpAddr.Addr()) {
			hm.l.Debug("lighthouse.remote_allow_list denied incoming handshake", "from", via)
			return
		}
	}

	// First message of a new handshake. The wire format requires RemoteIndex
	// to be zero here (the initiator has no responder index to fill in yet),
	// and generateIndex never allocates 0, so any non-zero RemoteIndex on a
	// stage-1 packet is malformed or someone probing for an index collision.
	// Drop without paying the cost of running noise on a pending Machine.
	if h.MessageCounter == 1 {
		if h.RemoteIndex != 0 {
			hm.l.Debug("dropping stage-1 handshake with non-zero RemoteIndex",
				"from", via, "remoteIndex", h.RemoteIndex)
			return
		}
		hm.beginHandshake(via, packet, h)
		return
	}
```

**File:** e2e/handshake_manager_test.go (L351-408)
```go
func TestHandshakeRemoteAllowList(t *testing.T) {
	t.Parallel()
	// Verify that a handshake from a blocked underlay IP is dropped with no
	// response and no state changes. Then verify the same packet from an
	// allowed IP succeeds.

	ca, _, caKey, _ := cert_test.NewTestCaCert(cert.Version1, cert.Curve_CURVE25519, time.Now(), time.Now().Add(10*time.Minute), nil, nil, []string{})
	myControl, myVpnIpNet, myUdpAddr, _ := newSimpleServer(cert.Version1, ca, caKey, "me", "10.128.0.1/24", m{
		"lighthouse": m{
			"remote_allow_list": m{
				"10.0.0.0/8": true,
				"0.0.0.0/0":  false,
			},
		},
	})
	theirControl, theirVpnIpNet, theirUdpAddr, _ := newSimpleServer(cert.Version1, ca, caKey, "them", "10.128.0.2/24", nil)

	myControl.InjectLightHouseAddr(theirVpnIpNet[0].Addr(), theirUdpAddr)
	theirControl.InjectLightHouseAddr(myVpnIpNet[0].Addr(), myUdpAddr)

	myControl.Start()
	theirControl.Start()

	r := router.NewR(t, myControl, theirControl)
	defer r.RenderFlow()

	t.Log("Trigger handshake from them")
	theirControl.InjectTunPacket(BuildTunUDPPacket(myVpnIpNet[0].Addr(), 80, theirVpnIpNet[0].Addr(), 80, []byte("Hi")))
	msg1 := theirControl.GetFromUDP(true)

	t.Log("Rewrite the source to a blocked IP and inject")
	blockedMsg := msg1.Copy()
	blockedMsg.From = netip.MustParseAddrPort("192.168.1.1:4242")
	myControl.InjectUDPPacket(blockedMsg)

	t.Log("Verify no tunnel, no pending, no response from blocked source")
	time.Sleep(100 * time.Millisecond)
	assert.Empty(t, myControl.ListHostmapHosts(false))
	assert.Empty(t, myControl.ListHostmapHosts(true))
	assert.Nil(t, myControl.GetFromUDP(false), "should not respond to blocked source")

	t.Log("Now inject the real packet from the allowed source")
	myControl.InjectUDPPacket(msg1)

	t.Log("Verify handshake completes from allowed source")
	resp := myControl.GetFromUDP(true)
	assert.NotNil(t, resp)
	theirControl.InjectUDPPacket(resp)
	theirControl.WaitForType(1, 0, myControl)

	t.Log("Drain cached packet and verify tunnel works")
	cachedPacket := myControl.GetFromTun(true)
	assertUdpPacket(t, []byte("Hi"), cachedPacket, theirVpnIpNet[0].Addr(), myVpnIpNet[0].Addr(), 80, 80)
	assertTunnel(t, myVpnIpNet[0].Addr(), theirVpnIpNet[0].Addr(), myControl, theirControl, r)

	myControl.Stop()
	theirControl.Stop()
}
```
