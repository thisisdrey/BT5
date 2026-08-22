### Title
Unvalidated `RelayFromAddr` in `handleCreateRelayRequest` lets an authenticated peer register relay state for an arbitrary victim VPN address - (File: `relay_manager.go`)

### Summary
`relayManager.handleCreateRelayRequest` authenticates the *sender* of a `CreateRelayRequest` control message via the established, Noise-authenticated tunnel (`h`), but it never checks that the `RelayFromAddr` (`from`) field embedded inside the message payload actually matches `h.vpnAddrs[0]`. This mirrors the reported `V3Vault::transform` flaw: the outer identity (`tokenId` / `h`) is validated, but an inner, attacker-supplied identity field encoded in the payload (`data` / `m.RelayFromAddr`) is used for a security-relevant action without being cross-checked against the authenticated caller.

### Finding Description
`relayManager.HandleControlMsg` dispatches `NebulaControl_CreateRelayRequest` messages to `handleCreateRelayRequest(v, h, f, msg)`, where `h` is the `HostInfo` of the peer that decrypted/authenticated this message over its own tunnel [1](#0-0) .

Inside `handleCreateRelayRequest`, the code extracts `from := protoAddrToNetAddr(m.RelayFromAddr)` and `target := protoAddrToNetAddr(m.RelayToAddr)` straight from the attacker-controlled payload [2](#0-1) . The only sanity check performed is whether `from` happens to equal *our own* address (to catch an accidental self-relay), not whether `from` equals `h.vpnAddrs[0]` (the actual sender) [3](#0-2) .

When `target` is our own address, the code immediately calls `AddRelay(rm.l, h, f.hostMap, from, &m.InitiatorRelayIndex, TerminalType, Established)`, installing relay state keyed by the attacker-chosen `from` address and marking it `Established` without any further verification that `h` is entitled to speak for that address [4](#0-3) . Any peer with a valid, established tunnel to us (i.e., anyone who has completed a normal handshake — no special privilege required) can set `RelayFromAddr` to any victim's VPN address.

The `PeerAddr` value stored on this relay (i.e., the unvalidated `from`) is later used directly by the data plane in `handleOutsideRelayPacket` to look up forwarding targets via `f.hostMap.QueryVpnAddrsRelayFor(hostinfo.vpnAddrs, relay.PeerAddr)` [5](#0-4) , so poisoned relay identity data directly influences real packet-forwarding decisions.

### Impact Explanation
This is a remote state-poisoning vulnerability reachable by any node holding a valid (but otherwise unprivileged) certificate-authenticated tunnel — no CA-signed certificate forgery is needed, only a normal legitimate handshake. By spoofing `RelayFromAddr` to a victim's overlay IP, an attacker can:
- Install bogus, immediately-`Established` relay entries for a victim's address on a peer that has direct connectivity to us, corrupting that peer's relay routing table for the victim's identity.
- Potentially cause traffic destined to the victim to be considered relayable through the attacker's tunnel instead of the legitimate path, since subsequent lookups (`QueryRelayForByIp`/`QueryVpnAddrsRelayFor`) key purely off the (unverified) IP field.
- Interfere with or hijack legitimate relay negotiation for the victim, since a genuine `CreateRelayRequest` from the victim later would collide with the attacker's already-`Established`, spoofed entry (index/state mismatch checks would then reject or confuse the victim's real requests, per the `Established`/`Disestablished` index-mismatch branches at lines 457-472).

### Likelihood Explanation
Any already-authenticated Nebula peer (which only requires a certificate signed by the shared CA, not any elevated privilege) can trigger this by sending a single crafted `NebulaControl_CreateRelayRequest` message with a forged `RelayFromAddr`. No race condition or unusual timing is required — the vulnerable code path is reached on every relay-request the node is willing to terminate (`f.myVpnAddrsTable.Contains(target)`).

### Recommendation
In `handleCreateRelayRequest`, validate that `m.RelayFromAddr` equals `h.vpnAddrs[0]` (or one of the sender's authenticated networks) before creating or updating any relay state on behalf of `from`. Reject the request (log and return) if the claimed `from` address does not correspond to the actual authenticated sender `h`, consistent with the mitigation applied for the analogous `V3Vault::transform` issue (validating that the caller-supplied identifier matches the identifier encoded in the payload before acting on it).

### Proof of Concept
1. Node `A` (attacker) and Node `C` (victim) both complete a normal handshake with Node `B`.
2. `A` sends `B` a `NebulaControl_CreateRelayRequest` with `RelayFromAddr = C`'s vpnAddr and `RelayToAddr = B`'s own vpnAddr, using an `InitiatorRelayIndex` of `A`'s choosing.
3. In `handleCreateRelayRequest`, `h` is `A`'s `HostInfo`, but `from` is parsed as `C`'s address; the only self-check (`f.myVpnAddrsTable.Contains(from)`) passes since `from` is `C`, not `B`.
4. Since `target == B`'s own address, `B` calls `AddRelay(rm.l, h, f.hostMap, from, &m.InitiatorRelayIndex, TerminalType, Established)`, creating an `Established` relay entry on `A`'s `HostInfo` keyed by `C`'s vpnAddr — even though `A` never proved control of `C`'s identity [4](#0-3) .
5. `B` replies to `A` with a `CreateRelayResponse`, completing the (illegitimate) relay from `B`'s perspective, poisoning `B`'s relay/forwarding state for `C`'s address.

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

**File:** relay_manager.go (L426-429)
```go
func (rm *relayManager) handleCreateRelayRequest(v cert.Version, h *HostInfo, f *Interface, m *NebulaControl) {
	//nil-checks for protoAddrToNetAddr handled by caller
	from := protoAddrToNetAddr(m.RelayFromAddr)
	target := protoAddrToNetAddr(m.RelayToAddr)
```

**File:** relay_manager.go (L439-444)
```go
	// Is the source of the relay me? This should never happen, but did happen due to
	// an issue migrating relays over to newly re-handshaked host info objects.
	if f.myVpnAddrsTable.Contains(from) {
		logMsg.Error("Discarding relay request from myself", "myIP", from)
		return
	}
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

**File:** outside.go (L206-216)
```go
	case ForwardingType:
		// Find the target HostInfo relay object
		targetHI, targetRelay, err := f.hostMap.QueryVpnAddrsRelayFor(hostinfo.vpnAddrs, relay.PeerAddr)
		if err != nil {
			hostinfo.logger(f.l).Info("Failed to find target host info by ip",
				"relayTo", relay.PeerAddr,
				"relayFrom", hostinfo.vpnAddrs[0],
				"error", err,
			)
			return
		}
```
