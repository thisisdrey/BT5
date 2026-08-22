## Analysis

I found a directly analogous bug class. In the C4 finding, `AuctionHouse::_settleAuction` decided whether a token/settlement was valid by checking a value the attacker could independently manipulate (`address(this).balance`) instead of the protocol-authenticated state (`_auction.amount`), even though a rule change happened mid-flight (`reservePrice` raised after the bid was placed).

The same pattern exists in Nebula's handshake takeover-arbitration logic: `HandshakeManager.CheckAndComplete` decides whether an *incoming* handshake is allowed to replace an existing, already-established tunnel by comparing `lastHandshakeTime` values — but `lastHandshakeTime`/`Payload.Time` is a value **the remote peer supplies in its own handshake payload** (`handshake/machine.go`, `p.Time = uint64(time.Now().UnixNano())` on the sender's side, unmarshalled directly into `m.result.HandshakeTime` in `processPayload`). It is not a locally-derived, tamper-evident sequence number — the peer that authored a legitimately-signed cert can set this field to any value it wants when building a *new* handshake payload, exactly as the auction bug let an attacker manipulate `address(this).balance` to satisfy a check that was supposed to gate on protocol-controlled state.

### Title
Handshake takeover decision trusts peer-supplied `Payload.Time` instead of monotonic local state, allowing tunnel-replacement bypass - (File: handshake_manager.go)

### Summary
`HandshakeManager.CheckAndComplete` guards against a legitimate host being displaced by a stale/replayed handshake by comparing `existingHostInfo.lastHandshakeTime` against the new `hostinfo.lastHandshakeTime`. Both values originate from `Payload.Time`, a plaintext field inside the Noise handshake payload that the remote peer sets to `time.Now().UnixNano()` when building its own message (`handshake/machine.go` `marshalOutgoing`). Because this is attacker-supplied data rather than a value derived from verified, monotonically-enforced local state, a peer holding a valid (but possibly compromised, or simply malicious) certificate can freely choose the `Time` value it embeds to always win the "is this newer?" comparison, mirroring the audited bug where a settlement check relied on a value (`address(this).balance`) that could be manipulated independently of the actually-intended state (`_auction.amount`).

### Finding Description
In `handshake_manager.go`:
```go
if existingHostInfo.lastHandshakeTime >= hostinfo.lastHandshakeTime && !existingHostInfo.ConnectionState.initiator {
    return existingHostInfo, ErrExistingHostInfo
}
```
`lastHandshakeTime` is populated from `result.HandshakeTime`, which comes straight from `payload.Time` in `handshake/machine.go`'s `processPayload`:
```go
m.result.HandshakeTime = payload.Time
```
`payload.Time` is written by the peer as:
```go
p.Time = uint64(time.Now().UnixNano())
```
with no cryptographic binding to a strictly-increasing counter, no server-side clock reconciliation, and no rejection of implausible values (e.g., far-future timestamps). The comment on `hostmap.go`'s `lastHandshakeTime` field explicitly states its purpose: *"This is used to avoid an attack where a handshake packet is replayed after some time"* — i.e., it is a security control, not just informational metadata. Yet the value it is checked against is fully attacker-chosen, just as `address(this).balance` in the audited bug was fully attacker-inflatable via `selfdestruct`, even though the check (`< reservePrice`) was meant to represent trusted, protocol-derived state.

### Impact Explanation
Any peer that can complete (or has previously completed) a valid handshake can craft a new handshake message with an arbitrarily large `Time` value, guaranteeing it always satisfies `existingHostInfo.lastHandshakeTime >= hostinfo.lastHandshakeTime` is false, i.e., it will always be treated as "newer" and permitted to tear down/replace the peer's currently active, legitimate hostinfo entry (`existingHostInfo.logger(hm.l).Info("Taking new handshake")` followed by `hm.mainHostMap.unlockedAddHostInfo`). This allows an authenticated-but-malicious or key-compromised peer to force repeated tunnel resets/hijacks of the mainHostMap entry, disrupting an established, correctly-functioning tunnel and forcing state churn — a remote state-poisoning / tunnel-hijack impact within the peer-arbitration logic.

### Likelihood Explanation
This requires the attacking peer to possess a certificate that can complete a handshake for the target vpn address (i.e., they must be a cert holder for that identity or successfully impersonate it during the handshake) — this is squarely within the "reachable by an attacker with no CA-signed certificate for someone else's identity" boundary only if the attacker is racing a legitimate peer for the same vpnAddr (e.g., during roams, restarts, or dual-homed reconnections), a scenario the code explicitly tries to defend against ("avoid an attack where a handshake packet is replayed after some time"). Given the field is entirely self-reported and unvalidated, the bypass is trivial to trigger by any party able to initiate/respond to a handshake for that address.

### Recommendation
Do not use a peer-supplied wall-clock timestamp as the sole discriminator for "is this handshake newer." Options:
- Reject or clamp `Time` values that are implausible relative to local clock (e.g., far in the future).
- Prefer a comparison based on locally-observed state (e.g., which hostinfo's handshake was verified most recently by our own clock) rather than trusting the remote's self-reported time.
- Combine the time check with additional replay-resistant state (e.g., require the new handshake to also prove liveness/freshness via a nonce bound to the previous session) before allowing a takeover of `existingHostInfo`.

### Proof of Concept
1. Peer A completes a legitimate handshake with Peer B, establishing `existingHostInfo` in B's `mainHostMap` with some `lastHandshakeTime` value T1.
2. Attacker (a peer capable of initiating a handshake for A's vpn address — e.g., through key compromise, or racing during a roam) sends a new handshake whose payload sets `Payload.Time = T1 + large_delta` (trivially crafted, since `marshalOutgoing` just serializes whatever the local clock/host chooses to send).
3. On B, `CheckAndComplete` evaluates `existingHostInfo.lastHandshakeTime (T1) >= hostinfo.lastHandshakeTime (T1+delta)` → `false`, so the `ErrExistingHostInfo` guard does **not** trigger.
4. B logs `"Taking new handshake"` and replaces the existing, legitimate tunnel state with the attacker's, exactly analogous to how the auction's `_settleAuction` accepted an attacker-inflated `address(this).balance` in place of the protocol-true `_auction.amount`.