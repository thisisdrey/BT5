Audit Report

## Title
Missing `Halted` operating-mode check in `submit_delivery_receipt` allows continued reward payout while the Snowbridge outbound pipeline is supposed to be stopped - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

## Summary
`pallet-snowbridge-outbound-queue-v2` defines a `Halted` error variant and an `OperatingModeChanged` event but never implements the corresponding `OperatingMode` storage item or `set_operating_mode` call, unlike its sibling `pallet-snowbridge-inbound-queue-v2`. As a result, the public `submit_delivery_receipt` extrinsic and its `process_delivery_receipt` payout logic have no way to be halted, even though the sibling inbound pallet explicitly gates its equivalent `submit` entrypoint with `ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted)`.

## Finding Description
In `pallet-snowbridge-inbound-queue-v2`, the `OperatingMode` storage value and `set_operating_mode` root call exist and are enforced at the top of `submit`: [1](#0-0) [2](#0-1) [3](#0-2) 

In `pallet-snowbridge-outbound-queue-v2`, the same `BasicOperatingMode` type is imported and an `OperatingModeChanged` event and a `Halted` error variant are declared, implying the same intended semantics, but no `OperatingMode` storage item or `set_operating_mode` call is defined anywhere in the pallet: [4](#0-3) [5](#0-4) [6](#0-5) 

The `submit_delivery_receipt` extrinsic and `process_delivery_receipt` logic perform verification, gateway-address checking, and reward payout via `T::RewardPayment::register_reward` with no halt gate at all: [7](#0-6) [8](#0-7) 

A repository-wide search confirms `Error::<T>::Halted` and `is_halted` are never referenced anywhere in `outbound-queue-v2`, meaning the `Halted` error variant is dead code — strong evidence the halt gate was intended but omitted. Checking the runtime wiring in `cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_to_ethereum_config.rs`, the `snowbridge_pallet_outbound_queue_v2::Config` implementation confirms there is no external `SafeMode`/call-filter mechanism specifically gating this pallet either: [9](#0-8) 

## Impact Explanation
This is a genuine implementation gap: the pallet was clearly designed to support a halt mechanism (matching event/error scaffolding exists) but the actual `OperatingMode` storage and enforcement were never wired up, unlike the sibling inbound pallet. This falls under "runtime bugs that compromise intended behavior" — governance has no pallet-native way to pause reward settlement (`register_reward`) and pending-order clearing (`PendingOrders::remove`) independent of external, non-pallet-local mitigations (e.g., filtering the call in the runtime's `BaseCallFilter`). The impact is limited to loss of a governance control lever over an already fee-funded payout mechanism (fees are pre-collected in `PendingOrders`, not minted), so this does not constitute theft/unbacked mint but does constitute a missing safety invariant on a state-mutating, fee-paying public entrypoint.

## Likelihood Explanation
High feasibility: `submit_delivery_receipt` is a normal signed extrinsic callable by anyone with a valid Ethereum event proof for a pending order; no privileged action is needed to trigger continued payout — only the absence of the intended pause mechanism.

## Recommendation
Add an `OperatingMode` storage item and `set_operating_mode` root call to `pallet-snowbridge-outbound-queue-v2` mirroring `pallet-snowbridge-inbound-queue-v2`, and gate `submit_delivery_receipt` (and ideally `do_process_message`) with `ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted)`, wiring up the already-declared but unused `Halted` error and `OperatingModeChanged` event.

## Proof of Concept
1. Confirm via code inspection that `pallet-snowbridge-outbound-queue-v2`'s storage section (lines 245-271) contains no `OperatingMode` value, contrasted with `pallet-snowbridge-inbound-queue-v2` lines 170-172/185-187/200-211.
2. Confirm via `grep` that `Error::<T>::Halted` and `is_halted` are never referenced in `outbound-queue-v2`, showing the declared `Halted` variant is dead code.
3. Write a unit test in the pallet's mock runtime: insert a `PendingOrders` entry, then call `submit_delivery_receipt` with a valid mocked proof/receipt at any point (simulating a scenario where governance intends the outbound pipeline halted) — observe that `process_delivery_receipt` always succeeds, calls `T::RewardPayment::register_reward`, and removes the order, with no available call to prevent this from within the pallet.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L170-172)
```rust
	/// The current operating mode of the pallet.
	#[pallet::storage]
	pub type OperatingMode<T: Config> = StorageValue<_, BasicOperatingMode, ValueQuery>;
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L185-187)
```rust
		pub fn submit(origin: OriginFor<T>, event: Box<EventProof>) -> DispatchResult {
			let who = ensure_signed(origin)?;
			ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted);
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L200-211)
```rust
		/// Halt or resume all pallet operations. May only be called by root.
		#[pallet::call_index(1)]
		#[pallet::weight((T::DbWeight::get().reads_writes(1, 1), DispatchClass::Operational))]
		pub fn set_operating_mode(
			origin: OriginFor<T>,
			mode: BasicOperatingMode,
		) -> DispatchResult {
			ensure_root(origin)?;
			OperatingMode::<T>::set(mode);
			Self::deposit_event(Event::OperatingModeChanged { mode });
			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L81-85)
```rust
use snowbridge_core::{
	digest_item::SnowbridgeDigestItem,
	reward::{AddTip, AddTipError},
	BasicOperatingMode,
};
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L219-230)
```rust
		/// Set OperatingMode
		OperatingModeChanged { mode: BasicOperatingMode },
		/// Delivery Proof received
		MessageDelivered { nonce: u64 },
	}

	#[pallet::error]
	pub enum Error<T> {
		/// The message is too large
		MessageTooLarge,
		/// The pallet is halted
		Halted,
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L245-271)
```rust
	/// Messages to be committed in the current block. This storage value is killed in
	/// `on_initialize`, so will not end up bloating state.
	///
	/// Is never read in the runtime, only by offchain message relayers.
	/// Because of this, it will never go into the PoV of a block.
	///
	/// Inspired by the `frame_system::Pallet::Events` storage value
	#[pallet::storage]
	#[pallet::unbounded]
	pub type Messages<T: Config> = StorageValue<_, Vec<OutboundMessage>, ValueQuery>;

	/// Hashes of the ABI-encoded messages in the [`Messages`] storage value. Used to generate a
	/// merkle root during `on_finalize`. This storage value is killed in `on_initialize`, so state
	/// at each block contains only root hash of messages processed in that block. This also means
	/// it doesn't have to be included in PoV.
	#[pallet::storage]
	#[pallet::unbounded]
	pub type MessageLeaves<T: Config> = StorageValue<_, Vec<H256>, ValueQuery>;

	/// The current nonce for the messages
	#[pallet::storage]
	pub type Nonce<T: Config> = StorageValue<_, u64, ValueQuery>;

	/// Pending orders to relay
	#[pallet::storage]
	pub type PendingOrders<T: Config> =
		StorageMap<_, Twox64Concat, u64, PendingOrder<BlockNumberFor<T>>, OptionQuery>;
```

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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_to_ethereum_config.rs (L194-220)
```rust
impl snowbridge_pallet_outbound_queue_v2::Config for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type Hashing = Keccak256;
	type MessageQueue = MessageQueue;
	// Maximum payload size for outbound messages.
	type MaxMessagePayloadSize = ConstU32<2048>;
	// Maximum number of outbound messages that can be committed per block.
	// It's benchmarked, including the entire process flow(initialize,submit,commit) in the
	// worst-case, Benchmark results in `../weights/snowbridge_pallet_outbound_queue_v2.
	// rs` show that the `process` function consumes less than 1% of the block capacity, which is
	// safe enough.
	type MaxMessagesPerBlock = ConstU32<32>;
	type GasMeter = ConstantGasMeterV2;
	type Balance = Balance;
	type WeightToFee = WeightToFee;
	type Verifier = EthereumBeaconClient;
	type GatewayAddress = EthereumGatewayAddress;
	type WeightInfo = crate::weights::snowbridge_pallet_outbound_queue_v2::WeightInfo<Runtime>;
	type EthereumNetwork = EthereumNetwork;
	type RewardKind = BridgeReward;
	type DefaultRewardKind = SnowbridgeReward;
	type RewardPayment = BridgeRelayers;
	type AggregateMessageOrigin = AggregateMessageOrigin;
	type OnNewCommitment = ();
	#[cfg(feature = "runtime-benchmarks")]
	type Helper = Runtime;
}
```
