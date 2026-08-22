### Title
Relay peer trusts attacker-controlled `RelayFromAddr` in `CreateRelayRequest` without verifying it against the authenticated sender, allowing remote relay-state poisoning for arbitrary third-party addresses - (File: relay_manager.go)

### Summary
`relayManager.handleCreateRelayRequest` derives the "on behalf of" address (`from`) for a relay solely from the attacker-controlled `RelayFromAddr`/`OldRelayFromAddr` field of an incoming `NebulaControl` message, and never checks that this claimed address matches the authenticated identity (`h.vpnAddrs`) of the peer that actually sent the message. Any node with a valid handshake to the target can therefore register itself in the target's hostmap/relay state as a `TerminalType` relay "for" an arbitrary victim vpn address it does not own, mirroring the reported bug class of trusting a caller-supplied "participator" identity instead of the real initiator.

### Finding Description
When node `h` sends a `CreateRelayRequest` control message and the receiver is the intended relay target (`f.myVpnAddrsTable.Contains(target)`), the code does this: [1](#0-0) [2](#0-1) 

`from` is read directly from the protobuf `RelayFromAddr` field supplied inside the (encrypted, but attacker-authored) control message body: `from := protoAddrToNetAddr(m.RelayFromAddr)` [3](#0-2) . The only sanity checks performed are that `from` is not one of my own addresses and that `target` is me [4](#0-3) . There is no check that `from == h.vpnAddrs[i]` for some `i`, i.e. no verification that the claimed relay beneficiary is actually the peer that authenticated and sent this control message (`h`), nor that `h` is a configured/authorized relay for that address.

As a result, when `h` is not the true owner of `from`, the code still calls `AddRelay(rm.l, h, f.hostMap, from, &m.InitiatorRelayIndex, TerminalType, Established)` [5](#0-4) , which inserts a `Relay{PeerAddr: from, Type: TerminalType, State: Established}` into `h.relayState` and into the global `hostMap.Relays` index [6](#0-5) . This is analogous to the smart-contract bug where an attacker embeds an arbitrary `userAddress` in a call routed through a trusted intermediary (the swap `Executor`) without the intermediary verifying that `userAddress` corresponds to the actual initiator; here `h` (the trusted, authenticated intermediary/direct peer) is allowed to assert an arbitrary victim identity (`from`) that is never cross-checked against `h`'s own authenticated identity.

By contrast, the forwarding branch of the same function (when `h` is asking me to relay to a third party) *does* derive `RelayFromAddr` from the authenticated sender's own address, `h.vpnAddrs[0]`, rather than trusting client-supplied data [7](#0-6) , confirming that the terminal branch's omission is an inconsistency/bug rather than intended design.

### Impact Explanation
An authenticated peer with a normal, valid-certificate handshake to a victim node can:
- Register itself in the victim's hostmap as an "Established" `TerminalType` relay for any arbitrary third-party VPN address (`from`), consuming a relay index slot and mutating `h.relayState`/`hostMap.Relays` on the victim (remote state poisoning), even though the claimed third party never requested or authorized this relay.
- Collide with a legitimate later `CreateRelayRequest` for the same `from` address: the `Established`/`Disestablished` branches compare `existingRelay.RemoteIndex != m.InitiatorRelayIndex` and, on mismatch, log an error and silently drop the request (`return`) instead of establishing/updating the relay [8](#0-7) . This can prevent the genuine relay-for-`from` peer from ever establishing/repairing its relay tunnel through the victim, resulting in denial of service for that third party's relayed connectivity, while the attacker's bogus mapping persists.
- Pollute administrative/diagnostic state (`sshPrintRelays`) with spoofed relay-for associations, complicating incident response.

This falls squarely in the allowed "hostmap/lighthouse/relay address trust" and "remote state poisoning" categories: an attacker with no CA-signed relationship to the victim address can still corrupt the victim's belief about who is relaying for whom, purely by supplying an unverified identity claim in an otherwise-authenticated control channel — the same "the intermediary never checked the claimed participant equals the real initiator" root cause as the referenced report.

### Likelihood Explanation
Exploitation only requires:
1. A valid Nebula certificate signed by the network's CA (any legitimate/authenticated but malicious node), and
2. A direct (non-relayed) handshake to the target node, and
3. Sending a crafted `NebulaControl` `CreateRelayRequest` message with `RelayFromAddr` set to any victim address and `RelayToAddr` set to the target's own address.

No cooperation from the claimed victim (`from`) is needed, and no special privileges (e.g. `am_relay`) are required to be the *target* of the terminal branch — only the sender needs a working direct tunnel to the target. This makes the likelihood high for any multi-node deployment that uses relays, since the check that's missing is a single identity comparison that is otherwise present (correctly) in the sibling forwarding-branch code path.

### Recommendation
In the terminal branch of `handleCreateRelayRequest`, require that the claimed `from` address is actually owned by the authenticated sender `h`, i.e. add a check equivalent to:
```go
if !slices.Contains(h.vpnAddrs, from) {
    logMsg.Error("RelayFromAddr does not match sender identity", "senderVpnAddrs", h.vpnAddrs)
    return
}
```
before calling `AddRelay`/`CompleteRelayByIP`, mirroring the correct behavior already used in the forwarding branch where `h.vpnAddrs[0]` (the authenticated sender's own address) is used instead of trusting the message body.

### Proof of Concept
1. Stand up nodes `A` (victim, has a legitimate handshake with `V` for other traffic, not relevant here), `V` (target/victim of this attack, `am_relay` irrelevant), and `M` (malicious, CA-signed, direct tunnel to `V`).
2. `M` completes a normal handshake with `V` so `V` has a `HostInfo` `h` for `M`.
3. `M` crafts a `NebulaControl` message: `Type = CreateRelayRequest`, `RelayFromAddr = <A's vpn address>`, `RelayToAddr = <V's vpn address>`, `InitiatorRelayIndex = <arbitrary>`, and sends it via `Control` message type to `V` over the direct `M<->V` tunnel.
4. On `V`, `HandleControlMsg` → `handleCreateRelayRequest` computes `from = A`, `target = V` (self); since `target` is `V`, and no existing relay for `A` exists on `h` (M's HostInfo), `AddRelay(..., h, ..., from=A, ..., TerminalType, Established)` is called, inserting a bogus `Established` relay-for-`A` record on `M`'s HostInfo in `V`'s hostmap — without `V` ever verifying that `M` is `A` or is authorized to relay for `A`.
5. Confirm via `sshPrintRelays` on `V` (or by inspecting `hostMap.Relays`) that `V` now believes `M` is an established terminal relay for `A`, and that a subsequent legitimate `CreateRelayRequest` from `A`'s real relay path with a different `InitiatorRelayIndex` is rejected (`"Existing relay mismatch with CreateRelayRequest"`), demonstrating the state-poisoning/DoS impact.

### Citations

**File:** relay_manager.go (L227-264)
```go
// AddRelay finds an available relay index on the hostmap, and associates the relay info with it.
// relayHostInfo is the Nebula peer which can be used as a relay to access the target vpnIp.
func AddRelay(l *slog.Logger, relayHostInfo *HostInfo, hm *HostMap, vpnIp netip.Addr, remoteIdx *uint32, relayType int, state int) (uint32, error) {
	hm.Lock()
	defer hm.Unlock()
	for range 32 {
		index, err := generateIndex(l)
		if err != nil {
			return 0, err
		}

		_, inRelays := hm.Relays[index]
		if !inRelays {
			// Avoid standing up a relay that can't be used since only the primary hostinfo
			// will be pointed to by the relay logic
			//TODO: if there was an existing primary and it had relay state, should we merge?
			if !hm.unlockedMakePrimary(relayHostInfo) {
				// The tunnel was torn down after the caller grabbed relayHostInfo. A relay standing
				// on an unlinked hostinfo would never carry traffic, and its Relays entry could
				// never be reclaimed since the delete-time cleanup has already run.
				return 0, errors.New("relay hostinfo is no longer in the hostmap")
			}

			hm.Relays[index] = relayHostInfo
			newRelay := Relay{
				Type:       relayType,
				State:      state,
				LocalIndex: index,
				PeerAddr:   vpnIp,
			}

			if remoteIdx != nil {
				newRelay.RemoteIndex = *remoteIdx
			}
			relayHostInfo.relayState.InsertRelay(vpnIp, index, &newRelay)

			return index, nil
		}
```

**File:** relay_manager.go (L426-487)
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

	logMsg.Info("handleCreateRelayRequest")
	// Is the source of the relay me? This should never happen, but did happen due to
	// an issue migrating relays over to newly re-handshaked host info objects.
	if f.myVpnAddrsTable.Contains(from) {
		logMsg.Error("Discarding relay request from myself", "myIP", from)
		return
	}

	// Is the target of the relay me?
	if f.myVpnAddrsTable.Contains(target) {
		existingRelay, ok := h.relayState.QueryRelayForByIp(from)
		if ok {
			switch existingRelay.State {
			case Requested:
				ok = h.relayState.CompleteRelayByIP(from, m.InitiatorRelayIndex)
				if !ok {
					logMsg.Error("Relay State not found")
					return
				}
			case Established:
				if existingRelay.RemoteIndex != m.InitiatorRelayIndex {
					// We got a brand new Relay request, because its index is different than what we saw before.
					// This should never happen. The peer should never change an index, once created.
					logMsg.Error("Existing relay mismatch with CreateRelayRequest",
						"existingRemoteIndex", existingRelay.RemoteIndex)
					return
				}
			case Disestablished:
				if existingRelay.RemoteIndex != m.InitiatorRelayIndex {
					// We got a brand new Relay request, because its index is different than what we saw before.
					// This should never happen. The peer should never change an index, once created.
					logMsg.Error("Existing relay mismatch with CreateRelayRequest",
						"existingRemoteIndex", existingRelay.RemoteIndex)
					return
				}
				// Mark the relay as 'Established' because it's safe to use again
				h.relayState.UpdateRelayForByIpState(from, Established)
			case PeerRequested:
				// I should never be in this state, because I am terminal, not forwarding.
				logMsg.Error("Unexpected Relay State found",
					"existingRemoteIndex", existingRelay.RemoteIndex,
					"state", existingRelay.State)
			}
		} else {
			_, err := AddRelay(rm.l, h, f.hostMap, from, &m.InitiatorRelayIndex, TerminalType, Established)
			if err != nil {
				logMsg.Error("Failed to add relay", "error", err)
				return
			}
		}
```

**File:** relay_manager.go (L576-579)
```go
		} else {
			req.RelayFromAddr = netAddrToProtoAddr(h.vpnAddrs[0])
			req.RelayToAddr = netAddrToProtoAddr(target)
		}
```
