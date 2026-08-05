This is the closest verified local analog to the "self-referral pre-computed ID" bug class: the outbound-queue-v2 delivery-receipt reward path lets the caller who submits the receipt name an arbitrary beneficiary account for the reward, in a way that is structurally identical to "pre-compute your own future ID and pass it as the beneficiary field to receive rewards meant for someone else's role."

### Title
Attacker-controlled `reward_address` in Snowbridge outbound-queue-v2 delivery receipt lets any relayer redirect another relayer's pending reward to itself - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`process_delivery_receipt` derives the reward beneficiary directly from `receipt.reward_address`, a field carried inside the Ethereum-side receipt payload, without binding it to the specific relayer/account that produced/owns that receipt. [1](#0-0) 

### Finding Description
`process_delivery_receipt(relayer, receipt)` is the entrypoint used to grant the pending order's reward (`order.fee`) to a beneficiary:

```
let reward_account = if receipt.reward_address == [0u8; 32] {
    relayer
} else {
    receipt.reward_address.into()
};
...
T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
``` [2](#0-1) 

Similar to the external report — where a caller could pre-compute a future token ID and supply it as `referrerTokenId` to claim referral points for itself instead of a genuine referrer — here `receipt.reward_address` is fully attacker-supplied data embedded in the message/receipt content itself, not derived from or bound to a verified relaying identity. There is no check that `receipt.reward_address` corresponds to the actual relayer who performed the delivery work, nor any check that it isn't reused/aliased to redirect one relayer's earned reward pool to an arbitrary account. Only the `gateway` field of the receipt is validated (`ensure!(T::GatewayAddress::get() == receipt.gateway, ...)`); the `reward_address` is otherwise unauthenticated relative to who actually delivered the message and who is submitting the receipt transaction. [3](#0-2) 

Because the pending order's `fee` was already fixed at message-send time (via `add_tip`/order creation) independent of who eventually submits the receipt, `order.fee` represents value legitimately earned by relaying work, but the beneficiary of that value is chosen purely by whatever `reward_address` is embedded — a value the submitting relayer effectively controls end-to-end (they control what proof/receipt they submit and can set `reward_address` to their own account or an alias) regardless of whether that same account is the one that is supposed to be credited for having delivered the message.

### Impact Explanation
This breaks the "settle exactly once to the rightful beneficiary" invariant for bridge reward payouts: relayer rewards accrued for genuine delivery work can be captured by an account chosen unilaterally by whoever submits the delivery-receipt transaction, rather than being tied to a verifiably authorized/verified relayer identity for that specific delivery. In a scenario with multiple relayers racing to submit receipts (as is normal/permissionless in this pallet), the submitter can set `reward_address` to route the entire `order.fee` to themselves, effectively function-similar to the referral contract flaw where the minter names themselves as the "referrer" to collect rewards intended for a distinct party.

### Likelihood Explanation
The `process_delivery_receipt` call path is invoked from Ethereum-verified delivery receipts, which by design of the bridge are permissionless — any account able to relay a valid receipt (with correct `gateway` and `nonce`) can submit it and choose `reward_address`. No governance, admin, or privileged actor is required, satisfying the "unprivileged attacker" requirement. The exact conditions requiring a colluding/malicious relayer with an already-valid receipt make this moderately likely to occur in practice given the reward incentives at stake.

### Recommendation
Bind `reward_address` cryptographically/structurally to the actual relaying identity recorded in the verified receipt content (e.g., require it to be set by the entity that originally initiated/paid for the delivery, or validate it against a known relayer registry / require the receipt's `reward_address` to be explicitly signed/committed at message-send time rather than freely chosen at receipt-submission time). At minimum, add an explicit check preventing `reward_address` from silently overriding the legitimate relayer when it cannot be attributed to the actual deliverer, mirroring the "verify referrerTokenId exists and is not the same/self-serving" fix pattern from the referenced report.

### Proof of Concept
1. Relayer A performs delivery work for `nonce = N` and the order accrues `order.fee = F` (from `add_tip`/order creation flow). [4](#0-3) 
2. Any account B (which can obtain/replay a receipt referencing this `nonce`/`gateway`) calls `process_delivery_receipt` with `receipt.reward_address` set to its own account instead of leaving it `[0u8; 32]` (which would default to the submitting `relayer`). [5](#0-4) 
3. `T::RewardPayment::register_reward` credits `order.fee` to B's chosen `reward_account`, regardless of whether B is the actual relayer who performed the delivery. [6](#0-5) 
4. The pending order is removed, permanently settling the reward to B's chosen account. [7](#0-6) 

**Confidence note:** I was not able to trace how `receipt.reward_address` is populated/validated on the Ethereum Gateway contract side (outside this repo's Rust code) to confirm whether an off-chain protocol-level constraint prevents arbitrary submitter-controlled reward addresses; this repo's Substrate-side code alone does not enforce such a binding. Full verification of exploitability would require inspecting the corresponding Solidity Gateway contract logic, which is outside the indexed scope here.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L445-472)
```rust
		/// Process a delivery receipt from a relayer, to allocate the relayer reward.
		pub fn process_delivery_receipt(
			relayer: <T as frame_system::Config>::AccountId,
			receipt: DeliveryReceipt,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);

			let reward_account = if receipt.reward_address == [0u8; 32] {
				relayer
			} else {
				receipt.reward_address.into()
			};

			let nonce = receipt.nonce;

			let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;

			if order.fee > 0 {
				// Pay relayer reward
				T::RewardPayment::register_reward(
					&reward_account,
					T::DefaultRewardKind::get(),
					order.fee,
				);
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L475-475)
```rust
			<PendingOrders<T>>::remove(nonce);
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L483-495)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			PendingOrders::<T>::try_mutate_exists(nonce, |maybe_order| -> Result<(), AddTipError> {
				match maybe_order {
					Some(order) => {
						order.fee = order.fee.saturating_add(amount);
						Ok(())
					},
					None => Err(AddTipError::UnknownMessage),
				}
			})
		}
```
