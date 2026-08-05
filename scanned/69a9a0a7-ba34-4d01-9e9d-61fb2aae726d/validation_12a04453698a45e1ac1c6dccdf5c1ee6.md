## Analysis

The Aave bug class ("owner/manager can redirect rewards to an arbitrary caller-supplied address") maps to a comparable pattern in Snowbridge's outbound message delivery-receipt flow, where the *recipient of an accumulated protocol reward* is derived from the identity of whoever happens to submit a public, replayable proof rather than from the party that is actually owed the fee.

### Title
Permissionless `submit_delivery_receipt` lets any account divert relayer rewards to itself when the Ethereum event's `reward_address` is zero - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`pallet_bridge_relayers`/Snowbridge outbound-queue-v2 pays a relayer reward once a `DeliveryReceipt` proof is submitted for a previously queued outbound message. The extrinsic `submit_delivery_receipt` is callable by *any signed account*, and the reward beneficiary falls back to that caller's own account whenever the verified Ethereum event's `reward_address` field is zero, instead of being bound to whoever actually incurred the Ethereum-side delivery cost.

### Finding Description
`submit_delivery_receipt` only checks that the caller is signed and that the supplied event log verifies against the light client (`T::Verifier::verify`) — it performs no check that the caller is the entity credited/entitled to the fee: [1](#0-0) 

The receipt is then processed in `process_delivery_receipt`, where the reward beneficiary is computed as: [2](#0-1) 

Note the fallback: `let reward_account = if receipt.reward_address == [0u8; 32] { relayer } else { receipt.reward_address.into() };`. When the Ethereum-side event carries a zero `reward_address` (`InboundMessageDispatched(nonce, topic, success, reward_address)`), the fee is paid to whichever account happened to submit the Substrate-side `submit_delivery_receipt` call — not to whoever actually paid for delivery on Ethereum. Because the underlying receipt/log data becomes public once mined and finalized on Ethereum (and the merkle/beacon proof for it is derivable by anyone from public chain state), any unprivileged account can construct the same `EventProof` and call `submit_delivery_receipt` themselves, claiming the `order.fee` registered in `PendingOrders` for a nonce they had no part in delivering.

This mirrors the Aave `claimRewards(address _to)` pattern: a caller-controlled destination for funds that belong to the protocol/another party, gated only by "does the caller hold some role" (here: "is signed") rather than "is the caller the entity actually owed the reward."

### Impact Explanation
This directly hits the "theft or unbacked mint or unlock" / "duplicate settlement or payout" impact category: WETH/DOT-denominated relayer fees accumulated in `PendingOrders` (funded from real bridge fee revenue) can be diverted away from the party that performed the Ethereum-side delivery work to an unrelated third party who merely resubmits public proof data. There is no requirement to be a "malicious relayer," "malicious peer," or governance actor — the attacker only needs to read finalized Ethereum chain data and submit an ordinary signed extrinsic on Bridge Hub.

### Likelihood Explanation
The likelihood is driven entirely by whether the emitted `reward_address` on the Ethereum Gateway side is left as zero for a given delivery (e.g., if the relaying party's Ethereum-side contract call doesn't explicitly set a reward address). In that case, the reward becomes a race to be first to relay the already-public proof to Bridge Hub — no privileged access, node compromise, or validator collusion needed, since the on-chain proof itself is the only requirement.

### Recommendation
Do not fall back to the extrinsic submitter's account as the reward beneficiary. Either:
- require `reward_address` to always be explicitly set and non-zero at the Ethereum Gateway contract level (reject/skip reward registration otherwise), or
- bind the reward strictly to the address embedded and authenticated in the verified Ethereum event, with no signer-based fallback, so that submitting the receipt is a purely permissionless "relay the proof" action that never changes who is economically entitled to the fee.

### Proof of Concept
1. A legitimate relayer delivers a message to the Ethereum Gateway without specifying (or with a zero) `reward_address`, causing the resulting `InboundMessageDispatched` event log to carry `reward_address = 0x00…00`.
2. The event is finalized on Ethereum; its receipt/log and the corresponding beacon/execution proof are now public and reconstructable by anyone.
3. An unrelated attacker account (never involved in delivering the message) builds the same `EventProof` from public Ethereum data and calls `EthereumOutboundQueueV2::submit_delivery_receipt(attacker_origin, event_proof)` on Bridge Hub before the legitimate relayer does, per the extrinsic definition: [3](#0-2) .
4. `process_delivery_receipt` falls back to crediting `reward_account = relayer` (the attacker), registering `order.fee` to the attacker via `T::RewardPayment::register_reward` as shown at [4](#0-3) , and the `PendingOrder` is removed, permanently denying the legitimate relayer their fee.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L453-473)
```rust
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
```
