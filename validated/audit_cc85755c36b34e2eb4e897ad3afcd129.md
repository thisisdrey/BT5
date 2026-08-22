### Title
Missing sender-identity validation in relay control-message handler allows relay state poisoning / spoofed relay establishment - (File: relay_manager.go)

### Summary
`relayManager.handleCreateRelayRequest` and `handleCreateRelayResponse` trust the `RelayFromAddr` / `RelayToAddr` fields carried inside an authenticated-but-attacker-influenced `NebulaControl` message body without cross-checking them against the actual, cert-verified identity of the sending peer (`h.vpnAddrs`). This mirrors the `[M-03]` bug class: a value that should be constrained/validated against an authoritative source (there: `msg.value` vs. the sum of `genesisCircSupply`+`collateral`; here: the claimed relay endpoint address vs. the sender's verified VPN address) is instead accepted at face value from attacker-controlled input, letting a legitimate-but-malicious tunnel peer install relay routing state for an address it does not own.

### Finding Description
`HandleControlMsg` in `relay_manager.go` unmarshals `NebulaControl` from an already-decrypted, already-authenticated tunnel (`h *HostInfo` is a peer that completed a Noise handshake and whose certificate was verified). It only checks that `RelayFromAddr`/`RelayToAddr` are non-nil: [1](#0-0) 

It then dispatches to `handleCreateRelayRequest`, which decodes `from` and `target` purely from the message body: [2](#0-1) 

The only identity check performed is whether `from` equals *my own* address (self-relay guard) and whether `target` equals my own address to decide the code path: [3](#0-2) 

Critically, there is no check anywhere in this function that `from` matches `h.vpnAddrs[0]` (i.e., the actual, cert-authenticated address of the peer `h` that sent this control message). This is exactly analogous to the referenced bug's flaw: two independent quantities (here, "who the sender authenticated as" vs. "who the sender claims the relay is `from`") are never reconciled against each other, so the second, attacker-supplied value is accepted unchecked. A peer with a valid certificate for address `X` can send a `CreateRelayRequest`/`CreateRelayResponse` claiming `RelayFromAddr = Y` (some other, potentially victim, VPN address) as long as `Y != f.myVpnAddrsTable` and `Y != from-is-not-caller-itself`. Since `AddRelay` and `h.relayState.InsertRelay(from, ...)` store relay state keyed by this unverified `from`/`target` address, an authenticated-but-malicious peer can:
- Register itself as a relay "for" an arbitrary victim VPN address it does not own, poisoning `h.relayState` and the `hostMap.Relays` index.
- Cause `handleCreateRelayResponse`'s middle-man logic to establish relay bindings between mismatched (attacker-chosen) addresses, since `relay.PeerAddr` and subsequent `QueryVpnAddr(relay.PeerAddr)` lookups are all seeded from the same unauthenticated field.

This is only reachable once the sender already completed a legitimate mutually-authenticated handshake with a valid certificate signed by a trusted CA — i.e., it requires being a legitimate-but-malicious peer in the mesh (not a fully unauthenticated outsider), similar to how the referenced Solidity bug required a caller (`msg.sender`) that could invoke `SubnetActorGetterFacet`. The privilege being bypassed is not "join the network" but "claim to relay for/from an address you were never certified for," which breaks the invariant that relay routing state must be anchored to certificate-verified VPN addresses (the same invariant class that `validatePeerCert`/`buildNetworks` elsewhere in the codebase are careful to enforce for direct traffic, e.g. via `f.myVpnAddrsTable.Contains(network.Addr())` self-checks and firewall address binding in `firewall.go` `Drop()`).

### Impact Explanation
A malicious-but-certificate-holding peer can poison relay routing tables (`RelayState.relayForByAddr`, `hostMap.Relays`) with entries that misattribute relay ownership to VPN addresses it does not control. Depending on downstream handling this can:
- Redirect or blackhole traffic intended for another peer through the attacker instead of the legitimate relay path.
- Create relay state desync between the "terminal" and "forwarding" ends of a relay chain (mismatched `PeerAddr`/`RemoteIndex` bindings), causing traffic to be forwarded to/associated with the wrong tunnel.
- Enable a limited traffic-forwarding/relay-hijack primitive within the mesh, since relay establishment ultimately decides where encrypted-but-still-routable frames get forwarded (`f.handleOutsideRelayPacket`, `handleCreateRelayResponse`'s peer-address lookups).

This does not directly break the Noise handshake authentication or certificate verification itself, but it subverts the invariant that relay state must be scoped to certificate-verified addresses, which is the closest reachable analog to the referenced report's "unbacked value accepted without cross-validation" bug class.

### Likelihood Explanation
Requires an attacker to already hold a CA-signed certificate and have completed a legitimate handshake with at least one victim peer (`am_relay` or simple mesh participant) — i.e., an authenticated-but-malicious insider, which is the strongest reachable threat model per the given constraints (no CA-signed cert would not reach this code path, since `HandleControlMsg` only runs over an established, authenticated `HostInfo`). Given that, the exploit path requires no race condition or timing — a single crafted `CreateRelayRequest`/`CreateRelayResponse` with a spoofed `RelayFromAddr`/`RelayToAddr` is sufficient, since nothing in `handleCreateRelayRequest`/`handleCreateRelayResponse` compares the claimed address to `h.vpnAddrs`.

### Recommendation
In `relayManager.handleCreateRelayRequest` and `handleCreateRelayResponse`, validate that the identity embedded in the control message aligns with the authenticated peer:
- Where the message asserts "I am relaying `from` address `from`", require `from` to be a member of `h.vpnAddrs` (the certificate-verified addresses of the connection the message arrived on), analogous to requiring `genesisCircSupply + collateral == msg.value` in the referenced bug — i.e., reconcile the attacker-supplied field against the authoritative, already-verified source of truth instead of trusting it unconditionally.
- Reject/log control messages where `RelayFromAddr` (for a request originating from `h`) doesn't match any address in `h.GetCert().Certificate.Networks()` / `h.vpnAddrs`.
- Apply the same reconciliation to `RelayToAddr` where applicable to the message semantics (e.g., in `handleCreateRelayResponse`'s middle-man branch, verify consistency between `m.RelayToAddr` and the `peerHostInfo` actually resolved).

### Proof of Concept
Deterministic code-level PoC could not be fully constructed within tool limits (no execution environment available), but the exploitable path is directly traceable in the source:
1. Attacker `E` completes a normal, cert-authenticated handshake with victim `V` (or acts as `am_relay`), producing a valid `HostInfo h` for `E` on `V`'s side.
2. `E` sends a `NebulaControl{Type: CreateRelayRequest, RelayFromAddr: <Victim2's address>, RelayToAddr: <V's address>}` over the established tunnel.
3. `HandleControlMsg` only checks the fields are non-nil: [1](#0-0) 
4. `handleCreateRelayRequest` decodes `from = Victim2's address` (not `E`'s real address) and, since `from != V`'s own address and isn't self, proceeds to call `AddRelay(rm.l, h, f.hostMap, from, &m.InitiatorRelayIndex, TerminalType, Established)` — binding relay state on `h` (which is `E`'s hostinfo) to `Victim2`'s VPN address, despite `E` never having proven ownership of `Victim2`'s certificate: [4](#0-3) 
5. This relay state is now used by `V` for future relay-related lookups (`QueryRelayForByIp`, `sshPrintRelays`, packet forwarding paths), all keyed on the spoofed `from` address rather than a certificate-verified one.

Because no execution/test harness was available in this session, this PoC is based on static code-path tracing rather than a running e2e test; a background engineering session with repo access could implement `relay_manager_test.go` unit test asserting `handleCreateRelayRequest` rejects a `RelayFromAddr` not present in `h.vpnAddrs` to confirm exploitability and validate the fix.

### Citations

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

**File:** relay_manager.go (L438-447)
```go
	logMsg.Info("handleCreateRelayRequest")
	// Is the source of the relay me? This should never happen, but did happen due to
	// an issue migrating relays over to newly re-handshaked host info objects.
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
