## Finding: Local Analog Found

### Title
Unbounded self-relaying drains Snowbridge inbound queue's per-message dusting reward from the destination parachain's sovereign account - (`File: bridges/snowbridge/pallets/inbound-queue/src/lib.rs`)

### Summary
The Snowbridge V1 inbound-queue pallet pays every message submitter a fixed "delivery cost" reward — which includes a constant dusting bonus (`PricingParameters::rewards.local`) unrelated to the message's actual value — out of the destination parachain's sovereign account. `submit` is a permissionless, `ensure_signed` extrinsic with no relayer whitelist and no check that the caller is a distinct, disinterested relayer. An ordinary user can generate their own cheap/empty Ethereum Gateway events and submit the corresponding proofs themselves, repeatedly draining the sovereign-account "vault" via self-dealing, exactly the broken invariant described in the SponsorVault report (no sybil resistance on a per-action dusting reimbursement funded from a shared vault).

### Finding Description
`Pallet::submit` in [1](#0-0)  is callable by any signed account (`ensure_signed`) — there is no requirement that the caller be a registered/whitelisted relayer, unlike `pallet-bridge-relayers` which gates priority boosts behind stake. After nonce/verification checks, the pallet unconditionally rewards `who` (the caller) from the destination parachain's sovereign account: [2](#0-1) 

The reward amount, `delivery_cost`, is computed as: [3](#0-2) 

This includes `T::PricingParameters::get().rewards.local`, a fixed per-message bonus independent of the message's real content or value — precisely the "dusting" pattern that LayneHaber (Connext) explicitly called out as inherently lacking sybil resistance. The pallet's own docs confirm the funding source is a shared sovereign account, not a per-message escrow tied to the sender: "The message relayers are rewarded using native currency from the sovereign account of the destination parachain" [4](#0-3) .

Nothing in `submit` ties the reward recipient (`who`) to being a third party distinct from the entity that emitted the Ethereum Gateway event. Any account can act as both the Ethereum-side "sender" (originating a minimal, near-free Gateway event, e.g. an empty/no-op channel message) and the Substrate-side "relayer" (submitting the corresponding light-client proof), collecting `delivery_cost` — including the fixed `rewards.local` component — every time, until the sovereign account's `reducible_balance` is exhausted (the only cap present, via `Preservation::Preserve`/`.min(delivery_cost)`).

### Impact Explanation
The destination parachain's sovereign account at the Bridge Hub is a shared fund meant to compensate honest relayers for real message-relay work. Because the reward is paid unconditionally per submitted proof and includes a flat bonus disconnected from message value, an ordinary unprivileged account can systematically drain this account by cheaply manufacturing and self-relaying many minimal messages. This is a fund-drain of parachain-controlled sovereign balances — value that was earmarked to sustain the bridge's relayer incentive — leaking out to a single self-dealing actor rather than to distinct honest relayers, degrading the bridge's economic sustainability and potentially exhausting funds needed for legitimate relayer compensation.

### Likelihood Explanation
Likelihood is high: no privileged role, governance action, validator, or compromised relayer is required. The attacker only needs an ordinary signed Substrate account and the ability to trigger the Gateway contract on Ethereum with a minimal-cost event (which the same actor controls end-to-end), then submit the light-client proof themselves. The only bound is the current sovereign-account balance, which is refilled through normal cross-chain fee flows, making repeated draining a persistent, low-cost economic attack rather than a one-off exploit.

### Recommendation
- Decouple the fixed dusting bonus (`rewards.local`) from unconditional per-`submit` payout; scale or cap it against real, verifiable relaying cost/value transferred, similar to how `pallet-bridge-messages`' `DeliveryPayments` only compensates the transaction fee actually incurred and confirmed valid messages.
- Consider requiring relayer registration/staking (as already implemented in `pallet-bridge-relayers`) before granting reward eligibility on `submit`, rather than allowing any signed account to both trigger and relay a message.
- Rate-limit or track reward payouts per origin/channel to detect and throttle self-dealing patterns, and let sovereign-account owners configure a maximum reimbursable amount, as the original SponsorVault report itself recommended.

### Proof of Concept
1. Attacker deploys/uses a controlled address to emit a minimal Gateway event on Ethereum (e.g., the cheapest valid payload accepted by `ConvertMessage`), incurring only Ethereum gas cost.
2. Attacker (or a colluding/same-controlled Substrate account) calls `submit(origin, event)` with a light-client proof of that event.
3. `T::Verifier::verify` succeeds (it is a real, valid Ethereum event/proof — no relayer-trust assumption is broken), and reference checks (`gateway`, `channel`, `nonce`) all pass because the attacker controls the entire message lifecycle.
4. `submit` unconditionally pays `delivery_cost` (weight fee + length fee + `rewards.local`) from the destination parachain's sovereign account to the caller, per lines [5](#0-4) .
5. Repeat with new nonces to continuously drain the sovereign account's `reducible_balance`, since no check exists to prevent the same actor from being both message-originator and reward-recipient across arbitrarily many low-value messages.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L10-11)
```rust
//! The message relayers are rewarded using native currency from the sovereign account of the
//! destination parachain.
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L237-243)
```rust
		pub fn submit(origin: OriginFor<T>, event: EventProof) -> DispatchResult {
			let who = ensure_signed(origin)?;
			ensure!(!Self::operating_mode().is_halted(), Error::<T>::Halted);

			// submit message to verifier for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L269-281)
```rust
			// Reward relayer from the sovereign account of the destination parachain, only if funds
			// are available
			let sovereign_account = sibling_sovereign_account::<T>(channel.para_id);
			let delivery_cost = Self::calculate_delivery_cost(event.encode().len() as u32);
			let amount = T::Token::reducible_balance(
				&sovereign_account,
				Preservation::Preserve,
				Fortitude::Polite,
			)
			.min(delivery_cost);
			if !amount.is_zero() {
				T::Token::transfer(&sovereign_account, &who, amount, Preservation::Preserve)?;
			}
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L343-349)
```rust
		pub fn calculate_delivery_cost(length: u32) -> BalanceOf<T> {
			let weight_fee = T::WeightToFee::weight_to_fee(&T::WeightInfo::submit());
			let len_fee = T::LengthToFee::weight_to_fee(&Weight::from_parts(length as u64, 0));
			weight_fee
				.saturating_add(len_fee)
				.saturating_add(T::PricingParameters::get().rewards.local)
		}
```
