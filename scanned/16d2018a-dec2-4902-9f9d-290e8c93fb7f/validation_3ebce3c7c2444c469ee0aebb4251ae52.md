## Title
No check that registered Snowbridge relayer reward matches actual escrowed fee, allowing reward-ledger inflation independent of real backing funds - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The Solidity report flags a missing guard: a withdrawal path computes `_amt` to send out without clamping it to `_before` (the contract's actual available balance), so a caller can request more than what is really backed. The direct analog in this repository is in `pallet_outbound_queue_v2::process_delivery_receipt` (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:445-480`), which credits a relayer's claimable reward ledger with `order.fee` — a value taken verbatim from user/relayer-supplied message data (via `do_process_message`, `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:343-443`, and `AddTip::add_tip`, lines 483-495) — with no check that this `fee`/tip value is bounded by, or actually backed by, real funds held for that purpose.

### Finding Description
`do_process_message` builds a `PendingOrder { nonce, fee, block_number }` directly from the `fee` field decoded out of the inbound `Message` (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:361` and `:431-436`). `AddTip::add_tip` further lets this `fee` be incremented arbitrarily via `order.fee.saturating_add(amount)` (lines 486-494) with only a non-zero check — no upper bound, no comparison against escrowed value.

When the relayer later submits a valid delivery receipt, `process_delivery_receipt` reads this `order.fee` and unconditionally calls:
```
T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
```
(`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:466-472`)

`register_reward` (→ `Pallet::register_relayer_reward` in `bridges/modules/relayers/src/lib.rs:399-432`) simply does `old_reward.saturating_add(reward_balance)` into the `RelayerRewards` storage map — an unconditional credit ledger entry with no verification that the rewards-paying account (`PayRewardFromAccount::rewards_account` / the Snowbridge AssetHub WETH reward pot) actually holds or was ever credited with a matching amount. This is structurally identical to the reported bug: the code computes an amount to be paid out later (`_amt`) without checking it against the actually available/escrowed balance (`_before`) at the point the obligation is created.

Because fee/tip values entering `PendingOrders` are driven by data that originates from the message sender (an XCM `InitiateTransfer`/outbound message constructed by any user sending assets through the bridge, or via the permissionless `add_tip`), nothing in this pipeline enforces that `order.fee` is capped by, or reconciled against, funds actually transferred into the reward-paying account for that nonce. The eventual `pay_reward` (`bp_relayers::PayRewardFromAccount::pay_reward`, `bridges/primitives/relayers/src/lib.rs:175-188`) will attempt a real `T::transfer` — if the aggregate of all registered-but-unpaid rewards across relayers/nonces exceeds the pot's real balance, first-claimers drain it and later legitimate claimants get `FailedToPayReward`/`FundsUnavailable`, permanently locking their rightfully earned reward, exactly mirroring the "_amt greater than _before" fund-availability defect from the report.

### Impact Explanation
This falls under "permanent user-fund or bridge-state lock" and "duplicate settlement/payout" risk categories from the impact gate: relayer rewards are registered as unconditional ledger credits disconnected from actual escrowed balance, so the reward accounting can promise more value than the paying account holds. Honest relayers who deliver messages later than an attacker who inflated their own registered fee/tip can find their legitimately earned reward un-payable, and the bridge's reward pot can be depleted disproportionately by inflated fee claims, degrading the intended relayer incentive mechanism that keeps message delivery (and thus bridge processing) running.

### Likelihood Explanation
Likelihood is moderate: reaching this path requires only the ability to send an outbound message through the bridge (any user with fee-paying capability) or invoke the permissionless `add_tip`, both of which are unprivileged public entry points — no malicious relayer, validator, or governance actor is required. The exact economic conditions needed to fully exhaust the reward pot depend on runtime-specific fee/tip bounds and pot funding cadence, which were not fully verifiable from the available index (fee/tip caps, if any, may be enforced upstream in the message-validation/fee-calculation logic that feeds `Message.fee`, which was not located in this scan).

### Recommendation
Before crediting `order.fee` (or accepting an `add_tip` amount) into the relayer reward ledger, validate it against the amount actually collected/escrowed for that specific nonce (e.g., verify that the reward-paying account's balance backing this order is at least `order.fee`, analogous to `if (_amt > _before) { _amt = _before; }`), or reconcile total outstanding `PendingOrders` fees against the pot balance before allowing further tips/fee registration, so registered rewards can never exceed real backing funds.

### Proof of Concept
Conceptual PoC (exact fee/tip caps not confirmed from available code):
1. Attacker submits an outbound message via the permissionless send path with an inflated `fee` value in the `Message` payload, or repeatedly calls `AddTip::add_tip(nonce, large_amount)` on a pending order they control the nonce context for.
2. `do_process_message` stores this in `PendingOrders` unchecked (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:431-436`).
3. Relayer (attacker or colluding) submits `submit_delivery_receipt` → `process_delivery_receipt`, which calls `register_reward(&reward_account, ..., order.fee)` unconditionally (lines 466-472), crediting `RelayerRewards` with the inflated amount.
4. Attacker calls `claim_rewards`/`claim_rewards_to` (`bridges/modules/relayers/src/lib.rs:224-235`) to trigger `pay_reward`, draining the shared rewards-account pot.
5. Subsequent legitimate relayers with correctly-earned, smaller registered rewards find `pay_reward` fails with `FundsUnavailable`/`FailedToPayReward`, permanently unable to claim already-registered rewards until the pot is refunded.

Note: I was unable to fully confirm from the indexed code whether an upstream fee-cap (e.g., a maximum bound tied to actually-transferred value in the inbound/outbound message) exists elsewhere in the Snowbridge fee-calculation pipeline that would mitigate this; this should be verified directly in a full checkout of `bridges/snowbridge/` before treating this as conclusively exploitable.