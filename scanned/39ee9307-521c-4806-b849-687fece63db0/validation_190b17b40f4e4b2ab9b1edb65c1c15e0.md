## Analysis

The external report's core broken invariant is: **a public, permissionless entry point pays out value to whichever caller happens to invoke it, using data that is trivially observable/copyable by anyone, so the "true" party doing useful work can be front-run for pure profit with no risk to the front-runner.** In `matchOrders()`, that data is the pair of opposing orders; the fix path recommended is to bind payout eligibility to a specific authorized party instead of "first tx wins".

The direct on-chain analog is `snowbridge_pallet_outbound_queue_v2::Pallet::submit_delivery_receipt` / `process_delivery_receipt`.

### Title
Unauthenticated reward fallback in `submit_delivery_receipt` lets any observer front-run and steal relayer compensation - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`submit_delivery_receipt` is a fully permissionless, `ensure_signed`-only extrinsic that anyone can call by supplying a merkle/receipt proof of a public Ethereum event log. The `InboundMessageDispatched` event carries a `reward_address` field that is supposed to identify the relayer who is owed the fee, but when that field is left as `[0u8; 32]`, the pallet silently redirects the reward to whichever account happens to submit the extrinsic on the parachain, rather than to the account that actually performed and paid for the Ethereum-side delivery.

### Finding Description
`process_delivery_receipt` computes the reward beneficiary purely from data that is public the moment the Ethereum delivery transaction is mined: [1](#0-0) 

```
let reward_account = if receipt.reward_address == [0u8; 32] {
    relayer
} else {
    receipt.reward_address.into()
};
...
if order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
```

The `event.reward_address` originates from the Ethereum-side `InboundMessageDispatched` log [2](#0-1)  — a value the message-delivering party sets on Ethereum but which is not cryptographically bound to who is allowed to submit the corresponding parachain proof. `T::Verifier::verify` only checks that the event log/proof is a valid, finalized Ethereum event [3](#0-2)  — it does not check that the caller `relayer` is the one who produced the delivery or is entitled to the reward. Since Ethereum transaction receipts and their logs are public as soon as they're included in a block, any unprivileged third party can construct the same merkle proof used by the honest relayer and race to submit `submit_delivery_receipt` first. If the honest relayer (who paid real Ethereum gas to dispatch the message) did not set `reward_address` (e.g., relies on the fallback-to-caller convenience default), the front-runner collects `order.fee` for free while paying only the parachain transaction fee — exactly the "caller only pays protocol/transaction fees, so front-running is almost always profitable" pattern described in the report, except here it results in outright theft of the reward rather than just a price-difference arbitrage.

Existing guards do not stop this: there is no check that `relayer == expected relayer`, no commit-reveal, no requirement that the submitter be a registered/whitelisted relayer (contrast with `pallet-bridge-relayers`' permissionless-lane registration model, which does bind claims to a registered account), and `PendingOrders` is removed unconditionally after the first successful (even racing) call [4](#0-3) , so there is no second chance for the true relayer to be paid once front-run.

### Impact Explanation
This directly causes theft / wrong-beneficiary payout of bridge relayer rewards funded from the parachain's reward pool: an unprivileged attacker with no special access (just an Ethereum full-node/RPC) can systematically harvest rewards intended for honest relayers whenever `reward_address` is unset, degrading the incentive model for Snowbridge delivery and potentially draining the reward-funding account over time. This matches the "theft ... duplicate settlement or payout" and "wrong beneficiary or amount" impact classes for Snowbridge delivery flow.

### Likelihood Explanation
High for any relayer that does not explicitly populate `reward_address`: no privileged role, governance, validator, or malicious-peer assumption is required — only a public Ethereum RPC endpoint and the ability to submit an ordinary signed parachain extrinsic (`ensure_signed`), which any account can do.

### Recommendation
Do not fall back reward attribution to `ensure_signed(origin)` of the parachain-side submitter. Require `reward_address` to always be explicitly set and non-zero on the Ethereum side, or bind the eligible claimant to the account that actually delivered on Ethereum (e.g., via a registered relayer identity check, analogous to `pallet_bridge_relayers`'s reward/claim model), so that submitting the proof is a purely mechanical, non-profitable act for anyone other than the entitled relayer.

### Proof of Concept
1. Honest relayer `Alice` delivers a message to the Ethereum Gateway without setting `reward_address` (defaults to zero), incurring real ETH gas cost.
2. The `InboundMessageDispatched` event (with `reward_address = 0`) is emitted and becomes publicly readable in the Ethereum block.
3. Attacker `Mallory`, monitoring Ethereum events, builds the same event-log + proof and calls `submit_delivery_receipt` on BridgeHub before `Alice` does.
4. `process_delivery_receipt` sees `receipt.reward_address == [0u8;32]` and sets `reward_account = Mallory` (the `ensure_signed` caller), paying `order.fee` to `Mallory` via `T::RewardPayment::register_reward` and removing the `PendingOrders` entry.
5. `Alice` receives nothing despite having performed and paid for the actual Ethereum delivery.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L298-317)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::submit_delivery_receipt())]
		pub fn submit_delivery_receipt(
			origin: OriginFor<T>,
			event: Box<EventProof>,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			let relayer = ensure_signed(origin)?;

			// submit message to verifier for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;

			let receipt = DeliveryReceipt::try_from(&event.event_log)
				.map_err(|_| Error::<T>::InvalidEnvelope)?;

			Self::process_delivery_receipt(relayer, receipt)
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L446-475)
```rust
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
			}

			<PendingOrders<T>>::remove(nonce);
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L10-27)
```rust
sol! {
	event InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address);
}

/// Delivery receipt
#[derive(Clone, Debug)]
pub struct DeliveryReceipt {
	/// The address of the outbound queue on Ethereum that emitted this message as an event log
	pub gateway: H160,
	/// The nonce of the dispatched message
	pub nonce: u64,
	/// Message topic
	pub topic: H256,
	/// Delivery status
	pub success: bool,
	/// The reward address
	pub reward_address: [u8; 32],
}
```
