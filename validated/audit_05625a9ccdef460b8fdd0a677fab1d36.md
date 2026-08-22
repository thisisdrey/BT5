### Title
Relay identity spoofing via unverified `RelayFromAddr` in `handleCreateRelayRequest` - (File: relay_manager.go)

### Summary
`relay_manager.go`'s `handleCreateRelayRequest` derives the relay's claimed source (`from`) directly from an attacker-controlled protobuf field in the `NebulaControl` message rather than from the sender's certificate-verified overlay address (`h.vpnAddrs`). This mirrors the `ZKPay` root cause: a value that should be derived from an already-authenticated source of truth (msg.value / the handshake-verified peer identity) is instead accepted as an unverified, caller-supplied parameter and used to drive privileged state changes.

### Finding Description
`handleCreateRelayRequest` unmarshals the relay-from address straight out of the message payload: [1](#0-0) 

The only sanity checks performed are whether `from` equals *this node's own* address (self-relay guard) and whether `target` equals this node's own address: [2](#0-1) 

There is no check that `from == h.vpnAddrs[0]` — i.e., no verification that the claimed relay-from address actually belongs to `h`, the peer whose identity was established via the Noise handshake and CA-verified certificate (`h.GetCert()`/`h.vpnAddrs`). As a result, an authenticated peer `H` can send a `CreateRelayRequest` claiming `RelayFromAddr = <victim's overlay IP>` while genuinely being some other node. The receiving node then creates relay state keyed on the *attacker-supplied* `from` value: [3](#0-2) 

and, in the forwarding branch, allocates forwarding relay state and issues `CreateRelayRequest`s toward the real target using this attacker-supplied identity as the "relayFrom": [4](#0-3) 

This is structurally the same bug class as the `ZKPay::query()` issue: a function assumes a caller-supplied value ("this is the amount you paid" / "this is the address I am relaying from") is trustworthy without validating it against the authenticated source of truth (`msg.value` / the cert-bound `vpnAddrs` of the actual sender `h`), and then uses that unverified value to create real state (a `QueryPayment` record / a `Relay` entry) that a later action can exploit.

### Impact Explanation
An authenticated attacker (any node holding a valid Nebula certificate, no special relay privileges required) can poison relay/hostmap state on a relay-capable node by claiming an arbitrary victim overlay address as the `RelayFromAddr`. This can be used to inject bogus relay routing entries associated with a victim's identity, potentially causing misrouted or attacker-influenced relay forwarding for that victim's address, and to build relay state under a spoofed "from" identity that the relay/forwarding node did not actually authenticate. This falls under remote state poisoning of hostmap/relay trust structures, reachable by any peer without special privileges.

### Likelihood Explanation
Any node that has completed a normal mutually-authenticated handshake (i.e., any peer with a valid, CA-signed certificate — the standard, no-special-privilege attacker model) can send a `Control`-type `NebulaControl_CreateRelayRequest` message with an arbitrary `RelayFromAddr`/`OldRelayFromAddr` value. The receiving node performs no cross-check against the authenticated sender's `vpnAddrs`, making this trivially reachable in a single message.

### Recommendation
Bind the relay's "from" identity to the cryptographically authenticated sender rather than trusting the message field: require `from == h.vpnAddrs[i]` for some `i` (i.e., the claimed relay-from address must be one of the sender `h`'s actual, cert-verified `vpnAddrs`), and reject the request otherwise, mirroring the fix pattern of validating a claimed value against the actually-authenticated source rather than accepting it as-is.

### Proof of Concept
1. Attacker node `A` establishes a normal, valid handshake with victim relay node `R` (attacker has a legitimate CA-signed cert for its own address `A_addr`).
2. `A` sends `R` a `Control` message of type `NebulaControl_CreateRelayRequest` with `RelayFromAddr` set to `victim_addr` (an address that is NOT `A`'s own `vpnAddrs`) and `RelayToAddr` set to some `target`.
3. In `handleCreateRelayRequest`, `from` is parsed as `victim_addr` [5](#0-4) ; the only self-check compares `from` against `R`'s own address table, which passes since `from` is the victim's address, not `R`'s.
4. `R` proceeds to build relay state (`AddRelay`) keyed on `victim_addr` as though the request legitimately originated from the victim, and may forward `CreateRelayRequest`s onward using this spoofed identity, without ever verifying `from` against `h.vpnAddrs` (the address actually proven during `A`'s handshake).

**Uncertainty note:** I could not fully trace every downstream consumer of the relay state created here (e.g., how `AddRelay`'s `from`-keyed entries later interact with `hostMap.QueryVpnAddrsRelayFor` and packet forwarding) within the available index; a full confirmation of end-to-end forwarding impact would benefit from a deeper session with complete file access.

### Citations

**File:** relay_manager.go (L426-436)
```go
func (rm *relayManager) handleCreateRelayRequest(v cert.Version, h *HostInfo, f *Interface, m *NebulaControl) {
	//nil-checks for protoAddrToNetAddr handled by caller
	from := protoAddrToNetAddr(m.RelayFromAddr)
	target := protoAddrToNetAddr(m.RelayToAddr)

	logMsg := rm.l.With(
		"relayFrom", from,
		"relayTo", target,
		"initiatorRelayIndex", m.InitiatorRelayIndex,
		"vpnAddrs", h.vpnAddrs,
	)
```

**File:** relay_manager.go (L441-447)
```go
	if f.myVpnAddrsTable.Contains(from) {
		logMsg.Error("Discarding relay request from myself", "myIP", from)
		return
	}

	// Is the target of the relay me?
	if f.myVpnAddrsTable.Contains(target) {
```

**File:** relay_manager.go (L481-487)
```go
		} else {
			_, err := AddRelay(rm.l, h, f.hostMap, from, &m.InitiatorRelayIndex, TerminalType, Established)
			if err != nil {
				logMsg.Error("Failed to add relay", "error", err)
				return
			}
		}
```

**File:** relay_manager.go (L541-558)
```go
		var index uint32
		var err error
		targetRelay, ok := peer.relayState.QueryRelayForByIp(from)
		if ok {
			index = targetRelay.LocalIndex
		} else {
			// Allocate an index in the hostMap for this relay peer
			index, err = AddRelay(rm.l, peer, f.hostMap, from, nil, ForwardingType, Requested)
			if err != nil {
				return
			}
		}
		peer.relayState.UpdateRelayForByIpState(from, Requested)
		// Send a CreateRelayRequest to the peer.
		req := NebulaControl{
			Type:                NebulaControl_CreateRelayRequest,
			InitiatorRelayIndex: index,
		}
```
