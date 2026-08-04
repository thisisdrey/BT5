## Verification

`submit_delivery_receipt` is a fully public, unprivileged extrinsic — any signed account can call it. It performs on-chain proof verification of the Ethereum event log (`T::Verifier::verify`), decodes it into a `DeliveryReceipt`, and then calls `process_delivery_receipt`, which reads `receipt.reward_address` directly out of the proven event and uses it verbatim as the payout beneficiary: [1](#0-0) [2](#0-1) 

### Title
Public `submit_delivery_receipt` binds relayer reward payout only to caller-supplied `reward_address`, allowing anyone to redirect another relayer's pending fee - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The redacted-cartel bug class is: a caller-controlled identifier/parameter (`briber`) is trusted by a public deposit function without binding it to the actual authorized party or to a consistent, expiry-checked identifier, letting an unrelated caller redirect the effect of the call (fund pull, or mismatched round/token bookkeeping). The same shape exists in `snowbridge-pallet-outbound-queue-v2`: `process_delivery_receipt` takes `receipt.reward_address` — a 32-byte field embedded in the *Ethereum-side* event payload — and uses it as the final Substrate-side reward beneficiary with no cross-check that this address corresponds to the account that is entitled to the fee for that specific `nonce`/order.

### Finding Description
`submit_delivery_receipt` is callable by any signed account (`ensure_signed(origin)?`) and only requires a valid Merkle/beacon proof of an Ethereum event log; it does not require the caller to be the relayer who actually delivered the message on Ethereum. The pallet then decodes `DeliveryReceipt` from the log and dispatches to `process_delivery_receipt`: [2](#0-1) 

```rust
pub fn process_delivery_receipt(...) -> DispatchResult {
    ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);
    let reward_account = if receipt.reward_address == [0u8; 32] {
        relayer
    } else {
        receipt.reward_address.into()
    };
    let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
    if order.fee > 0 {
        T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
    }
    <PendingOrders<T>>::remove(nonce);
    ...
}
```

The only invariants enforced are: (1) `gateway` address matches, (2) proof verifies, (3) `nonce` maps to a still-pending `PendingOrder`. There is no on-chain binding between `receipt.reward_address` and the identity of the account that performed the Ethereum-side delivery transaction, nor any check that the `origin`/`relayer` submitting the Substrate proof has any relationship to that address. Because the `reward_address` field lives inside data that is only checked for authenticity of *origin/gateway*, not for *who is entitled to the reward*, any actor who can construct or observe the delivery receipt (which is a public, on-chain Ethereum event, readable by anyone) can submit the proof first with an arbitrary `reward_address` filled by whoever crafted the underlying Ethereum-side call, effectively deciding who receives the `order.fee` originally set by the pallet at message-acceptance time (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` lines ~426-436, `PendingOrder { nonce, fee, block_number }`).

This mirrors the report's structural flaw precisely: the function trusts a caller-influenced identifier (`briber`/`reward_address`) to determine whose balance is affected, rather than deriving the beneficiary from data that is provably tied to the entity that earned the entitlement (the account that actually paid gas to deliver on Ethereum). `PendingOrders` removal happens exactly once per `nonce` (no double-settlement), so the "duplicate payout" sub-case does not apply here — but the "wrong beneficiary" sub-case does: fee flows to whatever `reward_address` is embedded in the log, and there is no cryptographic tie from that field back to a specific signer.

### Impact Explanation
If the `reward_address` in the Ethereum Gateway's delivery event is attacker-influenceable at the point the Ethereum transaction is submitted (e.g., it is a parameter of the delivery call rather than `msg.sender`), any address able to call the Ethereum-side delivery function can set `reward_address` to itself and claim rewards intended for whichever relayer/infrastructure was supposed to be compensated, or to an address disconnected from any registered/staked relayer, undermining the relayer incentive/registration model documented for `pallet_bridge_relayers` (stake, registration, priority boost). This degrades the intended "public underpriced work" economics of the bridge: anyone who races to deliver messages on Ethereum can also arbitrarily redirect the resulting on-chain reward, bypassing the reward-ledger's implicit assumption that the beneficiary corresponds to the actual delivering party.

### Likelihood Explanation
Medium: it requires an actor to be the one actually delivering (or racing to deliver) the message on the Ethereum Gateway contract, i.e., they are already an active permissionless relayer for that specific message — this is not a fully passive attack, but it needs no admin/governance/validator privilege and no compromised keys; it is exercisable by any unprivileged party willing to run the Ethereum-side delivery transaction and Substrate-side `submit_delivery_receipt` call, both of which are explicitly public/permissionless entrypoints in this pallet's design.

### Recommendation
Bind the reward beneficiary to a value that cannot be freely chosen independent of proof of actual work: either (a) restrict `reward_address` to only be honored when it matches a value cryptographically tied to the specific delivering account recorded by the Gateway contract (e.g., derived from `msg.sender` on Ethereum rather than an arbitrary call parameter), or (b) remove the `reward_address` override path entirely and always pay `relayer` (the account whose Substrate-side signature authorized the proof submission), unless a prior, separately-authorized `claim_rewards_to`-style redirection (as already implemented in `pallet_bridge_relayers::claim_rewards_to`) is used post-registration.

### Proof of Concept
1. A message with `fee = F` is accepted into `PendingOrders` at some `nonce` (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` lines 426-438).
2. Any account, call it `Attacker`, races to be the one delivering this message on the Ethereum Gateway, invoking the delivery call with `reward_address = Attacker`'s own address (assuming this field is settable independent of `msg.sender` on the Ethereum side — this specific detail could not be fully confirmed from the Rust-side repository alone, since the Solidity Gateway contract emitting this event is outside this repo's index).
3. `Attacker` (or anyone observing the resulting Ethereum event) submits `submit_delivery_receipt` with a valid proof of that event.
4. `process_delivery_receipt` verifies `gateway` and `nonce` only, then calls `T::RewardPayment::register_reward(&Attacker, ..., F)`, crediting the fee to `Attacker` regardless of whether `Attacker` bore any real relaying cost tied to a registered/staked relayer identity.

Note: full confirmation of whether the Ethereum Gateway contract lets the caller freely set `reward_address` independent of `msg.sender` requires inspecting the Solidity contract, which is not indexed in this repository; this is flagged as an open verification point rather than a certainty.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L446-480)
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

			Self::deposit_event(Event::MessageDelivered { nonce });

			Ok(())
		}
```
