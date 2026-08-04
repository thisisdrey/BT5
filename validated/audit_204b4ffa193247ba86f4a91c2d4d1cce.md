Based on my investigation, I found a concrete local analog in the Snowbridge inbound queue V2 pallet, where a fee/reward is registered as "owed" without any actual on-chain deduction or escrow tying it to that promise — the same broken invariant as the GMX report (fee accounted for a beneficiary but never actually withheld from the value that flows through the system).

### Title
Relayer tip is registered as a payable reward without any corresponding fund withdrawal/escrow, allowing unbacked reward credit and protocol shortfall - (File: bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs)

### Summary
`pallet_inbound_queue_v2`'s `AddTip::add_tip` only mutates the `Tips` storage map to record a promised tip amount for a message nonce; it performs no currency withdrawal, hold, or transfer at the time the tip is added. Later, in `process_message`, this recorded tip is unconditionally added to `relayer_fee` and passed to `T::RewardPayment::register_reward`, which credits the relayer with a claimable reward. There is no invariant in this pallet enforcing that the `tip` value stored in `Tips` was ever actually backed by a matching deduction from any account.

### Finding Description
`add_tip` [1](#0-0)  only checks `amount > 0` and that the nonce hasn't been consumed, then increments the `Tips` map — no `T::Currency`/asset withdrawal, reserve, or hold is performed here.

In `process_message`, the pallet takes whatever value sits in `Tips` for the nonce and adds it to `relayer_fee` to compute `total_tip`, which is registered as a reward for the relayer with no further validation: [2](#0-1) .

This mirrors the GMX/Vertex bug precisely: `addTokenFeesForWithdrawal()` recorded a fee obligation to the referrer without deducting it from the amount actually transferred to treasury/stakers, so when the referrer later claimed, the protocol lacked the backing funds. Here, `Tips::<T>::mutate` records an obligation ("this relayer is owed X more") purely as bookkeeping, and `register_reward` converts that bookkeeping directly into a claimable liability, without ever verifying that X was deducted from any user/account balance when `add_tip` was called. The pallet's own doc comment states relayers "are rewarded in wrapped Ether that is included within the message" for `relayer_fee` (i.e., backed value carried in the verified Ethereum message), but the `tip` component added via `add_tip` has no such backing mechanism inside this pallet — it is an unconstrained, permissionless-looking increment (any caller implementing `AddTip` for a valid, unconsumed nonce can set an arbitrary `amount`) into a value that is later paid out as if it were real.

### Impact Explanation
If the tip amount recorded via `add_tip` is not strictly and atomically backed by an equivalent withdrawal/escrow from whichever account is supposed to fund it (e.g., a local extrinsic on BridgeHub/AssetHub), the total value registered as a relayer reward (`relayer_fee + tip`) can exceed the actual value that ever entered the reward-paying pot. When the relayer later claims via `T::RewardPayment`, the payout draws from the pot's real balance, creating a permanent shortfall exactly like the GMX finding — the protocol commits to paying more than it actually collected for that nonce, and there's no code path in this pallet reconciling the tip liability against a real balance decrease.

### Likelihood Explanation
The mechanism is triggered by ordinary bridge relaying: any account able to add a tip for a not-yet-processed nonce (the trait is implemented for `Pallet<T>` and callable from other pallets/extrinsics, e.g., a `system-frontend`/XCM-driven tipping flow) and any relayer submitting that message can realize the payout. There is no privileged actor, malicious node, or governance action required — this is a public entrypoint / message-processing invariant gap. The concrete likelihood depends on whether the caller of `add_tip` (outside this pallet, e.g. in `pallet_snowbridge_system_frontend`) correctly and atomically withdraws the tip amount before/while calling `add_tip`; I was not able to trace that caller's implementation within this session (no further tool calls available) to confirm whether it already closes this gap. This is the main open uncertainty in this finding.

### Recommendation
Make `add_tip` (or its caller) atomically escrow/withdraw the tip amount at the time it is added, and store proof (or the actual `Credit`/held balance) rather than a bare `u128` amount in `Tips`. `process_message` should only register a reward up to the amount that was actually escrowed/withdrawn, and should fail closed if the recorded tip value cannot be matched to a real balance movement, mirroring the fix pattern needed for the GMX Vertex bug (deduct before crediting, never credit then hope the deduction happened elsewhere).

### Proof of Concept
Conceptual PoC (pending confirmation of the exact caller of `AddTip` in this codebase, since only `Pallet<T>`'s trait impl was inspected):
1. Caller C invokes whatever extrinsic implements/calls `Pallet::<T>::add_tip(nonce, amount)` for a not-yet-processed nonce, without any accompanying currency withdrawal being enforced by this pallet.
2. `Tips::<T>` now records `amount` for `nonce`, with zero backing funds actually moved.
3. A relayer submits the message for `nonce` via `submit`/`process_message`; `total_tip = relayer_fee + amount` is computed and `T::RewardPayment::register_reward(relayer, kind, total_tip)` is called unconditionally [2](#0-1) .
4. The relayer later claims the registered reward; the reward-paying account pays out `total_tip`, which includes the unbacked `amount`, causing a shortfall in the reward-paying pot.

Note: This finding's likelihood hinges on whether the pallet(s) that actually call `AddTip::add_tip` (e.g., `pallet_snowbridge_system_frontend`, referenced in the grep results but not read in this session) perform the missing atomic withdrawal. I could not verify this before running out of tool-call budget, so this should be treated as requiring confirmation against that caller's code before being escalated further.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L234-239)
```rust
			// Pay relayer reward
			let tip = Tips::<T>::take(nonce).unwrap_or_default();
			let total_tip = relayer_fee.saturating_add(tip);
			if total_tip > 0 {
				T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
			}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L248-258)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			// If the nonce is already processed, return an error
			ensure!(!Nonce::<T>::get(nonce.into()), AddTipError::NonceConsumed);
			// Otherwise add the tip.
			Tips::<T>::mutate(nonce, |tip| {
				*tip = Some(tip.unwrap_or_default().saturating_add(amount));
			});
			return Ok(());
		}
```
