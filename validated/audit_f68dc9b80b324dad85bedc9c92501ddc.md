This is the strongest analog found: `handleRecvError` in `outside.go` tears down an authenticated, established tunnel based on an **unauthenticated, unencrypted `RecvError` packet** — the same access-control-level mismatch as the original finding (a low-trust actor performing a high-trust "halt" action).

### Title
Unauthenticated `RecvError` packet allows a no-cert attacker to tear down an established tunnel - (File: outside.go)

### Summary
The original finding is about a privileged "halt protocol" action (`setHalted()`) being reachable by an under-privileged role (`onlyStrategist`) instead of the properly-privileged one (`onlyGovernance`). The reachable nebula analog is `Interface.handleRecvError`, which processes the plaintext, unauthenticated `header.RecvError` packet type and, on a coarse address match, immediately tears down a fully-authenticated tunnel — an action that should only be triggerable by a party that has actually completed the AEAD-authenticated handshake/data-plane exchange for that index.

### Finding Description
In `outside.go`, `readOutsidePackets` dispatches `header.RecvError` packets before any certificate or encryption check is performed: [1](#0-0) 

`handleRecvError` then looks up the hostinfo purely by the numeric `RemoteIndex` carried in the plaintext header, and gates the teardown only on a same-`netip.AddrPort` check: [2](#0-1) 

The `RemoteIndex` is a 32-bit value handed out during the initial handshake and observable to any on-path or address-spoofing attacker who can see this host's traffic (e.g., watches earlier handshake packets, which are also sent in plaintext). An attacker who can spoof source `UDP` address/port to match the current registered remote of the victim's tunnel (`hr == addr`) — or who is genuinely positioned on that path without holding any Nebula certificate at all — can force `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`, tearing down the tunnel state entirely. This is the same class of bug as the report: the "halt/teardown" action is gated by a UDP-address equality check (a low-assurance, spoofable check analogous to `onlyStrategist`) rather than by anything cryptographically bound to the peer's CA-signed certificate (the `onlyGovernance`-equivalent guarantee that only the properly-authenticated party can trigger destructive protocol state changes).

Note that there is a config guard, `acceptRecvErrorConfig.ShouldRecvError(addr)`, but by default this accepts `RecvError` packets ("always" per the changelog entry), so the destructive path is reachable without any certificate-based authentication in the default configuration: [3](#0-2) 

### Impact Explanation
An attacker with no CA-signed certificate who can inject or spoof a single UDP packet toward a victim (matching the victim's currently registered remote address/port for a given index) can force teardown of an active, authenticated Nebula tunnel. This is a remote state-poisoning / denial-of-service impact: the victim's tunnel and pending handshake state are deleted (`closeTunnel` + `DeleteHostInfo`), forcing renegotiation and disrupting connectivity — directly analogous to the "malicious actor forces a halt" impact in the original report, except here the trigger requires no privileged role or certificate at all, only knowledge of the numeric index and a spoofed source address.

### Likelihood Explanation
Likelihood is moderate-to-high in adversarial network positions: `RemoteIndex` values are exchanged in earlier plaintext handshake packets and are not secret, and UDP source address spoofing (or genuine on-path presence) is a well-known low-barrier capability for a network-adjacent attacker. The default configuration accepts `RecvError` packets ("always"), so no special victim configuration is required.

### Recommendation
Do not allow a fully-unauthenticated, unencrypted control packet type to directly destroy authenticated tunnel state. At minimum:
- Require `RecvError` handling to be rate-limited and/or to require some proof tied to the current session (e.g., only honor it during clearly transient states, not to instantly delete established `ConnectionState`), rather than the current spoofable `addr == hr` check.
- Default `firewall`/`accept_recv_error` policy to a stricter mode than "always", and clearly document that `always` trusts the network layer for this destructive path.
- Consider authenticating `RecvError` (e.g., embedding an AEAD tag keyed off the existing session state) so it can't be forged by an off-path attacker who merely guesses/observes the index and spoofs the source address.

### Proof of Concept
1. Establish a normal tunnel between `myControl` and `theirControl` (as in `TestCloseTunnelAuthenticated`, which demonstrates the sibling `CloseTunnel` packet path already requires the AEAD-authenticated `ConnectionState` — contrast with `RecvError`, which requires no such authentication): [4](#0-3) 
2. As an attacker with no certificate, craft a plaintext `header.RecvError` packet (`header.Encode(..., header.RecvError, 0, remoteIndex, 0)`, matching the shape built in `sendRecvError`): [5](#0-4) 
3. Send it from a spoofed UDP address/port equal to the victim's currently registered remote for that hostinfo (`hr`). `handleRecvError` will pass the `hr == addr` check and immediately call `closeTunnel` + `DeleteHostInfo`, tearing down the tunnel with no certificate or cryptographic proof of authorization.

### Citations

**File:** outside.go (L76-84)
```go
	switch h.Type {
	case header.Handshake:
		f.handshakeManager.HandleIncoming(via, packet, h)
		return

	case header.RecvError:
		f.handleRecvError(via.UdpAddr, h)
		return
	}
```

**File:** outside.go (L528-539)
```go
func (f *Interface) sendRecvError(endpoint netip.AddrPort, index uint32) {
	f.messageMetrics.Tx(header.RecvError, 0, 1)

	b := header.Encode(make([]byte, header.Len), header.Version, header.RecvError, 0, index, 0)
	_ = f.outside.WriteTo(b, endpoint)
	if f.l.Enabled(context.Background(), slog.LevelDebug) {
		f.l.Debug("Recv error sent",
			"index", index,
			"udpAddr", endpoint,
		)
	}
}
```

**File:** outside.go (L541-575)
```go
func (f *Interface) handleRecvError(addr netip.AddrPort, h *header.H) {
	if !f.acceptRecvErrorConfig.ShouldRecvError(addr) {
		f.l.Debug("Recv error received, ignoring",
			"index", h.RemoteIndex,
			"udpAddr", addr,
		)
		return
	}

	if f.l.Enabled(context.Background(), slog.LevelDebug) {
		f.l.Debug("Recv error received",
			"index", h.RemoteIndex,
			"udpAddr", addr,
		)
	}

	hostinfo := f.hostMap.QueryReverseIndex(h.RemoteIndex)
	if hostinfo == nil {
		f.l.Debug("Did not find remote index in main hostmap", "remoteIndex", h.RemoteIndex)
		return
	}

	hr := hostinfo.GetRemote()
	if hr.IsValid() && hr != addr {
		f.l.Info("Someone spoofing recv_errors?",
			"addr", addr,
			"hostinfoRemote", hr,
		)
		return
	}

	f.closeTunnel(hostinfo)
	// We also delete it from pending hostmap to allow for fast reconnect.
	f.handshakeManager.DeleteHostInfo(hostinfo)
}
```

**File:** CHANGELOG.md (L128-131)
```markdown
### Added

- Add a config option to control accepting `recv_error` packets which defaults to `always`. (#1569)

```

**File:** e2e/tunnels_test.go (L522-547)
```go
	myHi := theirControl.GetHostInfoByVpnAddr(myVpnIpNet[0].Addr(), false)
	if myHi == nil {
		t.Fatal("There is no hostinfo for my tunnel")
	}
	r.Log("It does")

	buf := make([]byte, 1024)
	hdr := header.H{
		Version:        1,
		Type:           header.CloseTunnel,
		Subtype:        0,
		Reserved:       0,
		RemoteIndex:    hi.RemoteIndex,
		MessageCounter: 5,
	}
	out, err := hdr.Encode(buf)
	if err != nil {
		t.Fatal(err)
	}

	pkt := &udp.Packet{
		To:   hi.CurrentRemote,
		From: myHi.CurrentRemote,
		Data: out,
	}
	r.InjectUDPPacket(myControl, theirControl, pkt)
```
