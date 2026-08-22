### Title
`remote_allow_list` underlay-IP restriction is completely bypassed for relayed handshakes - (File: `handshake_manager.go`)

### Summary
`HandshakeManager.validatePeerCert` enforces the `lighthouse.remote_allow_list` (an operator-configured restriction on which underlay/physical network locations are permitted to originate a handshake) only when the handshake was received directly, guarded by `if !via.IsRelayed`. When a handshake arrives via a relay (`via.IsRelayed == true`), this check is skipped entirely, so the underlay-address trust control is never applied to relayed peers.

### Finding Description
`validatePeerCert` is the function responsible for checking a peer certificate for self-connection and underlay-address trust before a handshake is allowed to proceed: [1](#0-0) 

The `AllowAll` check against `f.lightHouse.GetRemoteAllowList()` is the only enforcement point for the `remote_allow_list` restriction on incoming handshakes, and it is explicitly gated by `!via.IsRelayed`. This mirrors the reported bug class: “a check is being performed…but it could be done better” — here, the check exists and is correct for the direct path, but the guard condition allows an entire class of connections (all relayed handshakes) to skip it outright, rather than validating them against the same policy (e.g., validating the relay's or the ultimate peer's underlay address). [2](#0-1)  shows the intended behavior for the direct case: a handshake from a blocked underlay IP is dropped. There is no equivalent test or enforcement path exercising the relayed case, and the code structurally cannot enforce it since the check is unconditionally skipped.

### Impact Explanation
`remote_allow_list` is a nebula security control meant to restrict which underlay (physical/internet) network locations are trusted to establish a tunnel — e.g., an operator can require that all nebula peers connect only from a corporate egress range, blocking connections that originate from arbitrary public internet addresses even if they hold a valid certificate. By routing a handshake through any available relay node, a peer whose underlay address would otherwise be rejected can bypass this restriction entirely, since the relayed path never calls `AllowAll`. This is a firewall/address-trust enforcement bypass (remote state poisoning / unauthorized network admission) explicitly within the class of “hostmap/lighthouse/relay address trust” bypasses.

### Likelihood Explanation
Exploitation only requires the ability to route a handshake via an existing relay node (`relay.am_relay` peers are commonly deployed to help NAT traversal) and does not require any additional privilege beyond what is already needed to reach a relay — the attacker never needs to satisfy the `remote_allow_list` policy itself. Since relays are a standard, commonly-enabled feature, the bypass path is easily reachable in normal deployments that rely on `remote_allow_list` for underlay trust enforcement.

### Recommendation
Apply the `remote_allow_list` check (or an equivalent policy check against the relay's/peer's effective underlay address) unconditionally, regardless of `via.IsRelayed`. If relayed handshakes are intended to be exempt by design, this should be an explicit, documented policy decision validated against the relay node's own underlay address rather than a silent skip, and the relay node itself should be required to already satisfy `remote_allow_list` for the traffic it forwards.

### Proof of Concept
1. Configure host `me` with `lighthouse.remote_allow_list` set to deny the address range that `them` (the attacker) resides in, e.g. `{"attacker_range/x": false, "0.0.0.0/0": true}`.
2. Verify directly: `them` attempts a direct handshake to `me`; per `validatePeerCert`, `!via.IsRelayed` is true, `AllowAll` is evaluated, and the handshake is rejected (as in `TestHandshakeRemoteAllowList`, `e2e/handshake_manager_test.go:351-408`).
3. Introduce a `relay` node that both `me` and `them` can reach, and have `them` initiate the handshake via the relay (`via.IsRelayed == true`), analogous to `TestHandshakeRelayComplete` (`e2e/handshake_manager_test.go:521-568`).
4. Because `validatePeerCert` skips the `AllowAll` check entirely when `via.IsRelayed` is true, the handshake from the disallowed underlay address completes successfully, demonstrating the bypass of the `remote_allow_list` policy that direct connections are subject to.

### Citations

**File:** handshake_manager.go (L1030-1036)
```go
	if !via.IsRelayed {
		if !f.lightHouse.GetRemoteAllowList().AllowAll(vpnAddrs, via.UdpAddr.Addr()) {
			f.l.Debug("lighthouse.remote_allow_list denied incoming handshake",
				"vpnAddrs", vpnAddrs, "from", via)
			return nil, false, false
		}
	}
```

**File:** e2e/handshake_manager_test.go (L351-366)
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
```
