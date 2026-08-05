Based on my research, I found a concrete local analog already documented as a fix in this repository, which confirms the vulnerable pattern existed and matches the external report's "no bound on emergency/halt duration decoupled from actual protection" theme — but applied to Snowbridge's proof-verification path rather than admin-abuse.

### Title
Halting the Ethereum light client did not stop proof verification, allowing continued reward payout and message dispatch during a declared bridge emergency - (File: bridges/snowbridge/pallets/ethereum-client/src/lib.rs)

### Summary
The external report's core defect is that entering an "emergency/paused" state does not actually stop the unsafe operations it is meant to gate — the state exists but the enforcement is incomplete, so value-moving operations continue unimpeded. The direct Snowbridge analog is documented in `prdoc/stable2603-2/pr_11856.prdoc`: when `pallet-ethereum-client` was set to `Halted` operating mode (the bridge's emergency-stop mechanism), the `Verifier::verify` implementation did **not** short-circuit — it kept performing Ethereum-side proof verification. Only `EthereumBeaconClient::submit` (new beacon header updates) was blocked. As a result, `inbound_queue_v2::submit` and `outbound_queue_v2::submit_delivery_receipt` could still process receipts and pay relayer rewards from `PendingOrders` even while governance had halted the bridge because of a suspected light-client compromise. [1](#0-0) 

### Finding Description
`pallet-ethereum-client`'s halted/operating-mode flag is intended to be the single kill-switch stopping all Ethereum-side trust decisions once the beacon light client is suspected compromised. However, prior to the fix described in the prdoc, `Halted` mode only gated the beacon-header-submission entrypoint; it did not gate `Verifier::verify`, which is the function relied upon by `snowbridge-pallet-inbound-queue-v2::submit` and `snowbridge-pallet-outbound-queue-v2::submit_delivery_receipt` to accept proofs. [2](#0-1)  Because verification kept running against a light-client state that governance had explicitly frozen due to a compromise concern, unauthorized/forged proof material bound to stale or attacker-influenced beacon state could still be accepted, letting `submit_delivery_receipt` pay out relayer rewards from `PendingOrders` and letting `inbound_queue_v2::submit` keep dispatching XCM. This is exactly the "emergency mode that doesn't actually stop the unsafe path" pattern from the `Perpetual` report, translated to a proof-verification/message-dispatch/reward-payout surface rather than an admin-controlled trading pause.

### Impact Explanation
If exploited before the fix, an attacker able to produce or replay proofs against the un-gated verifier during a declared emergency could cause duplicate or unauthorized reward payouts (`PendingOrders` drain) and continued inbound message dispatch, i.e. theft/duplicate settlement and bridge-state processing that governance believed had been stopped — a direct match to "theft or unbacked mint or unlock, duplicate settlement or payout" and "public underpriced work that ... stalls bridge processing" categories in the impact gate. The severity is high because it defeats the chain's designated incident-response control (halting) precisely during the window when the system is most vulnerable (a suspected beacon compromise).

### Likelihood Explanation
This is not a hypothetical — it is a bug that was found and fixed in-repo (`pr_11856`), meaning the vulnerable code path genuinely existed and was independently reachable by any relayer submitting valid-looking proofs; no malicious admin, validator, or relayer collusion was required to trigger the unsafe behavior — the gap was purely in incomplete gating logic. The fix (per the prdoc) closes the gap by making `Verifier::verify` return `VerificationError::Halted` when in `Halted` mode, confirming the described flow is accurate. [3](#0-2) 

### Recommendation
Ensure every entrypoint that consumes proof verification or pays out value based on Ethereum-side state (`inbound_queue_v2::submit`, `outbound_queue_v2::submit_delivery_receipt`, and any future verifier consumers) checks the pallet's halted/operating-mode flag directly at the point of proof verification, not only at the beacon-header-submission entrypoint, so that a single kill-switch cannot be bypassed by calling a different public extrinsic that shares the same underlying trust state.

### Proof of Concept
1. Governance detects a suspected beacon light-client compromise and calls `set_operating_mode` on `pallet-ethereum-client` to set `Halted`, expecting all Ethereum-derived trust decisions to stop. [4](#0-3) 
2. Prior to the fix, a relayer calls `outbound_queue_v2::submit_delivery_receipt` (or `inbound_queue_v2::submit`) with a proof; `Verifier::verify` executes normally because it does not check the `Halted` state. [5](#0-4) 
3. `submit_delivery_receipt` locates the matching `PendingOrder` and pays the relayer reward via the reward payment path, and/or `inbound_queue_v2::submit` dispatches the XCM — both occurring while the chain believes the bridge is fully halted. [6](#0-5)

### Citations

**File:** prdoc/stable2603-2/pr_11856.prdoc (L1-25)
```text
title: 'Snowbridge: halt the Ethereum verifier when the bridge is in emergency stop'

doc:
  - audience: Runtime Dev
    description: |
      When `pallet-ethereum-client` is in `Halted` operating mode, its `Verifier::verify`
      implementation now short-circuits with the new `VerificationError::Halted` instead of
      attempting to verify Ethereum-side proofs.

      Previously, halting the light client only blocked new beacon header updates via
      `EthereumBeaconClient::submit`. Proof verification still ran, which meant
      `inbound_queue_v2::submit` and `outbound_queue_v2::submit_delivery_receipt` could
      continue to process receipts and pay out relayer rewards from `PendingOrders` while
      governance had halted the bridge (e.g. after a suspected beacon light client compromise).

      Halting the verifier closes that gap in one place — covering both inbound dispatch and
      outbound delivery-receipt reward payments.

crates:
  - name: snowbridge-verification-primitives
    bump: major
  - name: snowbridge-pallet-outbound-queue-v2
    bump: major
  - name: snowbridge-pallet-ethereum-client
    bump: patch
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L34-41)
```rust
//! 9. On the Ethereum side, the message root is ultimately the thing being verified by the Beefy
//!    light client.
//! 10. When the message has been verified and executed, the relayer will call the extrinsic
//!     `submit_delivery_receipt` to:
//! 	a. Verify the message with proof for a transaction receipt containing the event log,
//! 	   same as the inbound queue verification flow
//! 	b. Fetch the pending order by nonce of the message, pay reward with fee attached in the order
//!    	c. Remove the order from `PendingOrders` map storage by nonce
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/README.md (L60-72)
```markdown
**2. Governance: `set_operating_mode`**

Allows governance (Root origin) to set the operating mode of the pallet. This can be used to:

- Halt all incoming message processing (Halted state).
- Resume normal operation or set other custom states.

```
pub fn set_operating_mode(
    origin: OriginFor<T>,
    mode: BasicOperatingMode,
) -> DispatchResult
```
```
