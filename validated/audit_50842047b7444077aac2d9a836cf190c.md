### Title
Unauthenticated relay identity binding via spoofed `RelayFromAddr` in `handleCreateRelayRequest` - ([File: relay_manager.go])

### Summary
`relayManager.handleCreateRelayRequest` derives the `from` vpnAddr solely from the attacker-supplied `m.RelayFromAddr` field of an unauthenticated `NebulaControl_CreateRelayRequest` message and, when no existing relay state exists, calls `AddRelay(rm.l, h, f.hostMap, from, &m.InitiatorRelayIndex, TerminalType, Established)` without ever checking that `from` is one of `h.vpnAddrs`. This lets any peer `h` that has completed a legitimate handshake (i.e. any unprivileged but certificate-holding node) install `Established` relay state on itself for an arbitrary victim vpnAddr it does not own.

### Finding Description
In `relay_manager.go`, `HandleControlMsg` unmarshals the control message and only checks that `RelayFromAddr`/`RelayToAddr` are non-nil [1](#0-0) ; it performs no check that `RelayFromAddr` actually corresponds to the sending hostinfo `h`. `handleCreateRelayRequest` then computes `from := protoAddrToNetAddr(m.RelayFromAddr)` [2](#0-1)  and, when the request's target is "me" and no existing relay state is found for `from`, calls:

```go
_, err := AddRelay(rm.l, h, f.hostMap, from, &m.InitiatorRelayIndex, TerminalType, Established)
``` [3](#0-2) 

`AddRelay` itself never validates `vpnIp` (here `from`) against `relayHostInfo.vpnAddrs` (here `h.vpnAddrs`); it simply allocates an index and inserts `Relay{Type, State, LocalIndex, PeerAddr: vpnIp}` into `h.relayState` [4](#0-3) . Only the check `if f.myVpnAddrsTable.Contains(from)` (rejecting self) and `f.myVpnAddrsTable.Contains(target)` (routing decision) are performed [5](#0-4)  — neither validates `from` against `h`'s certificate-bound vpnAddrs. Because the control channel message is delivered over an already-established, encrypted tunnel to hostinfo `h`, the message is "authenticated" only in the sense that it came from `h`; the payload fields `RelayFromAddr`/`RelayToAddr` are attacker-controlled and are trusted at face value, directly contradicting the invariant that peer VPN addressing must be bound to the peer's certificate (verified at handshake time), not to arbitrary self-reported fields in a control message.

The result is that `h.relayState` (the state that governs the peer-address→relay-index mapping used to decide whether relayed traffic identified as coming "from" a given vpnAddr through `h` is trusted at `Established` state) can be poisoned by `h` itself to claim ownership of a victim's real vpnAddr, entirely bypassing certificate-based address verification.

### Impact Explanation
This is a remote state-poisoning bug: an authenticated-but-unprivileged peer can cause the local relay logic (`h.relayState`) to bind an `Established` relay entry for a victim vpnAddr that `h` does not own. Since relay/forwarding logic elsewhere in the codebase (e.g. `StartRelays`, `SendVia`) uses `relayState`/`PeerAddr` entries keyed by vpnAddr to route or accept relayed handshake/traffic without further address verification, this allows the attacker's hostinfo to impersonate the victim's address in the relay subsystem, potentially enabling traffic misdirection, relay hijacking, or corruption of legitimate relay setup for the victim (denial-of-service/wedge for the victim's real relay path). This matches Nebula's "remote state poisoning" and "firewall/identity enforcement bypass" bounty impact categories, scoped to the relay subsystem rather than full traffic decryption.

### Likelihood Explanation
Fully feasible for any unprivileged peer that has completed a legitimate handshake with the target node (a precondition explicitly allowed by the rules — no CA control, no key compromise, no lighthouse compromise needed). The attacker only needs to send one crafted `NebulaControl_CreateRelayRequest` control message over its own already-established tunnel with `RelayFromAddr` set to any victim vpnAddr; no races or timing are required, and it is fully repeatable/deterministic.

### Recommendation
In `handleCreateRelayRequest` (and `EstablishRelay`/`handleCreateRelayResponse` where similar trust of `RelayFromAddr` occurs), validate that the `from` address extracted from `m.RelayFromAddr`/`m.OldRelayFromAddr` is actually contained in `h.vpnAddrs` (the certificate-verified addresses for the peer that sent the control message) before calling `AddRelay(..., Established)`. Reject the request (log and return) if `from` is not one of `h.vpnAddrs`.

### Proof of Concept
Add a unit test in `relay_manager_test.go` (or extend `control_test.go`) that:
1. Sets up two hostinfos: `attacker` (h) with `vpnAddrs = [attackerAddr]`, and simulate the local node as the relay target.
2. Constructs a `NebulaControl{Type: NebulaControl_CreateRelayRequest, RelayFromAddr: netAddrToProtoAddr(victimAddr), RelayToAddr: netAddrToProtoAddr(myVpnAddr), InitiatorRelayIndex: 1}` where `victimAddr` is NOT in `attacker.vpnAddrs`.
3. Call `rm.handleCreateRelayRequest(cert.Version2, attackerHostInfo, f, msg)`.
4. Assert: `attackerHostInfo.relayState.QueryRelayForByIp(victimAddr)` returns `ok == false`, OR if the call is allowed to proceed for compatibility, assert that no entry with `State == Established` and `PeerAddr == victimAddr` is created when `victimAddr` is absent from `attackerHostInfo.vpnAddrs`.

Current code fails this assertion: `AddRelay` unconditionally inserts `Established` relay state for `from` (`victimAddr`) into `attackerHostInfo.relayState` regardless of `attackerHostInfo.vpnAddrs` contents.

### Citations

**File:** relay_manager.go (L229-264)
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
```

**File:** relay_manager.go (L320-334)
```go
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
```

**File:** relay_manager.go (L426-429)
```go
func (rm *relayManager) handleCreateRelayRequest(v cert.Version, h *HostInfo, f *Interface, m *NebulaControl) {
	//nil-checks for protoAddrToNetAddr handled by caller
	from := protoAddrToNetAddr(m.RelayFromAddr)
	target := protoAddrToNetAddr(m.RelayToAddr)
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
