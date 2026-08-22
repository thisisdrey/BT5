### Title
Unauthenticated `RelayFromAddr` in `CreateRelayRequest` lets a tunneled peer register relay state under a forged vpn address - ([File: relay_manager.go])

### Summary
`relayManager.handleCreateRelayRequest` (relay_manager.go:426-488, "target is me" branch) derives `from` solely from the attacker-controlled `m.RelayFromAddr`/`m.OldRelayFromAddr` protobuf fields and uses it directly as the key for `h.relayState` via `AddRelay(rm.l, h, f.hostMap, from, &m.InitiatorRelayIndex, TerminalType, Established)`. There is no check that `from` matches any address in `h.vpnAddrs` (the sending HostInfo's own certified identity), so any already-tunneled peer can register relay bookkeeping under an arbitrary claimed identity.

### Finding Description
`handleCreateRelayRequest` receives `h` — the `HostInfo` for the already-authenticated, already-tunneled sender — and `m`, a decrypted but otherwise attacker-controlled `NebulaControl` message. `from`/`target` are computed purely from `m.RelayFromAddr`/`m.RelayToAddr` (or the v1 integer equivalents) at relay_manager.go:428-429, with no cross-check against `h.vpnAddrs`: [1](#0-0) 

In the "target is me" branch, when no existing relay state exists for `from`, the code calls: [2](#0-1) 

`AddRelay` then inserts the relay directly into `h.relayState`, keyed by the attacker-supplied `from`, not by any value drawn from `h.vpnAddrs`: [3](#0-2) 

Because `h` is a real, cert-authenticated `HostInfo` (it must be, or `HandleControlMsg`/decryption would never have delivered `m`), the sender is legitimately who they claim via the handshake — but that handshake only certifies `h.vpnAddrs`, not the value the sender chooses to place in `RelayFromAddr`. The code conflates "peer we have a validated Noise session with" (`h`) with "vpn address named in `from`", and stores relay state keyed to the latter without ever comparing it to `h.vpnAddrs`. `AddRelay` unconditionally trusts the caller-supplied `vpnIp` argument.

Existing checks (myVpnAddrsTable.Contains, state-machine transitions for `Requested`/`Established`/`Disestablished`) only guard against self-relay and index reuse; none of them validate that `from` is actually one of `h`'s certified addresses.

### Impact Explanation
This is a remote relay-state poisoning issue: an unprivileged, already-tunneled peer can cause the relay host to record `h.relayState`/`hm.Relays[index]` entries associating an arbitrary, unrelated vpn address with `h`. Any later logic that looks up "who is the certified owner of relay traffic destined for address X" via `QueryRelayForByIp`/`hm.Relays[index]` will resolve to the attacker's session `h` for an address the attacker was never certified for, enabling relay-path/traffic-steering confusion (data destined for/claiming to be from a different vpn address gets bound to the attacker's tunnel). This matches "remote state poisoning" in the accepted impact categories, though it does not itself break the underlying Noise/cert-based end-to-end encryption between the true endpoints.

### Likelihood Explanation
Fully reachable by any already-handshaked peer with no special privilege: they only need a live Noise session (an ordinary certified tunnel) and the ability to send a `header.Control` / `NebulaControl_CreateRelayRequest` packet, which is standard relay-negotiation traffic. No CA control, spoofed source, or additional certificate is required — just setting `RelayFromAddr` to a value different from one's own `vpnAddrs`. This is trivially repeatable.

### Recommendation
In `handleCreateRelayRequest` (and symmetrically in `handleCreateRelayResponse`/`EstablishRelay`), before trusting `from`/`m.RelayFromAddr`, verify that `from` is contained in `h.vpnAddrs` (i.e., `slices.Contains(h.vpnAddrs, from)`) for the "target is me" branch, and reject/log-and-drop the message otherwise. Bind all `AddRelay`/`InsertRelay` calls made off `h` to `h.vpnAddrs`-derived values instead of raw protobuf-provided addresses, so relay bookkeeping is authenticated by certified identity rather than attacker-supplied fields.

### Proof of Concept
Unit test in `relay_manager_test.go` style:
1. Construct a `HostInfo` `h` with `h.vpnAddrs = [A]` and a valid `relayState`.
2. Construct `f.myVpnAddrsTable` containing `target` (so the "target is me" branch is taken).
3. Build `msg := &NebulaControl{Type: NebulaControl_CreateRelayRequest, RelayFromAddr: netAddrToProtoAddr(B), RelayToAddr: netAddrToProtoAddr(target)}` where `B != A`.
4. Call `rm.HandleControlMsg(h, msg.Marshal(), f)`.
5. Assert that `h.relayState.QueryRelayForByIp(B)` returns `ok == false` (i.e., no relay entry should be created for an address not in `h.vpnAddrs`), and that instead either the message is dropped or the relay is bound to `A`.
Currently this assertion fails: `QueryRelayForByIp(B)` succeeds and returns a `Relay{PeerAddr: B, ...}` attached to `h`, proving the forged identity is accepted into relay bookkeeping.

### Citations

**File:** relay_manager.go (L229-267)
```go
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
	}

	return 0, errors.New("failed to generate unique localIndexId")
```

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
