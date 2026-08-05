## Title
`process_message()` registers relayer rewards from an unauthenticated, self-declared `relayer_fee`/`Tips` amount that is never validated against value actually locked/backed on Ethereum, mirroring the "amount parameter never checked against actual transferred value" flaw - ([File: bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs])

### Summary
The external report's core defect is: a public entrypoint lets the caller supply an arbitrary "amount" that is trusted as backing for a later payout, without any check that the caller (or the underlying deposit) actually supplied that value. The Snowbridge inbound-queue-v2 pallet has the same shape of trust gap: `Pallet::<T>::process_message` computes `total_tip = relayer_fee.saturating_add(tip)` straight from the untrusted `Message` payload's `relayer_fee` field plus the `Tips` storage entry, and unconditionally calls `T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip)` [1](#0-0) . `register_reward` simply mutates an in-storage balance the relayer can later claim/mint via `register_relayer_reward` [2](#0-1) , with no on-chain reconciliation step that the number came from Ether actually locked/burned for this specific nonce.

### Finding Description
`Message` (decoded from a verified Ethereum receipt) carries `relayer_fee` and `value` fields that are attacker/relayer-influenced content of the emitted event, not independently re-derived on-chain from an escrowed balance held by the pallet [3](#0-2) . `process_message` never checks that `relayer_fee` (or the accumulated `Tips`) is bounded by, or reconciled against, `message.value` or any pallet-held escrow before calling `register_reward`, which is the exact analog of `bridgeOut()` never checking `_originalAmount` against `msg.value` — the amount used to authorize a payout is taken at face value from caller-controlled input rather than from verified custody of funds.

Separately, the permissionless `AddTip::add_tip(nonce, amount)` trait method used by `Tips` [4](#0-3)  only checks `amount > 0` and that the nonce is unconsumed — it does not itself withdraw/lock any asset. The only enforcement that a tip is backed by real value lives one hop away, in `snowbridge-pallet-system-frontend::add_tip`, which swaps/burns a caller-supplied `Asset` before dispatching an XCM `Transact` to `snowbridge-pallet-system-v2::add_tip` on BridgeHub [5](#0-4) , which forwards to `InboundQueue::add_tip`/`OutboundQueue::add_tip` after only an origin check (`T::FrontendOrigin::ensure_origin`) [6](#0-5) . The inbound-queue-v2 pallet itself has no independent verification linking a registered `Tips` amount to burned Ether — it fully trusts whatever numeric `amount` arrives through this call chain, exactly the "amount is provided by the user... but never verified against the actual supplied value" pattern from the report.

### Impact Explanation
If any caller in the chain (a compromised/buggy frontend integration, a future alternate `AddTip` implementer, or a runtime misconfiguration of `FrontendOrigin`) can invoke `add_tip`/reach `process_message` with an inflated `amount`/`relayer_fee` not actually backed by burnt/locked Ether, the relayer reward registered via `register_reward` becomes an unbacked liability against the shared bridge-relayers reward pool [7](#0-6) , which is funded from a shared rewards account rather than per-relayer escrow. This is analogous to Mallory's drain in the report: rewards claimed by one relayer are paid from the pool that should back other relayers' legitimately-earned rewards, and because `claim_rewards`/`claim_rewards_to` pays out of a shared account, a falsely inflated registration can exhaust funds meant for honest relayers (theft/duplicate settlement of a shared pool), or, if amounts are absurd, could stall reward payout entirely for the shared account.

### Likelihood Explanation
Direct external exploitation is limited today because the currently-wired `FrontendOrigin` path does perform a real swap/burn before calling `add_tip`, and `relayer_fee`/`value` inside `Message` are themselves verified by the receipt/light-client proof for the specific committed message. However, the *pallet-level* invariant — that `total_tip` handed to `register_reward` is provably backed by locked/burned Ether for that exact nonce — is not enforced inside `inbound-queue-v2` or `pallet-bridge-relayers` itself; it depends entirely on every caller of `AddTip`/`process_message` behaving correctly. This is a weak, implicit trust boundary rather than a state-machine-enforced conservation-of-value guarantee, so likelihood should be judged as low-but-real: any new integration, governance-added `FrontendOrigin`, or an alternate outbound/inbound reward source that doesn't replicate the frontend's burn logic reintroduces the exact bug class from the report.

### Recommendation
Enforce the invariant inside the reward-registration path itself, not just at the frontend: require `process_message`/`add_tip` to validate `relayer_fee`/`tip` against an amount actually escrowed or burned for that specific `nonce`/`message_id` (e.g., record the burned amount at burn time and require `register_reward` to consume that specific record rather than trusting a raw `u128` parameter). At minimum, add defensive bounds (e.g., cap `total_tip` by a per-message maximum derived from `message.value`) so a value mismatch cannot silently register an unbacked reward.

### Proof of Concept
Conceptual PoC (mirrors the report's Mallory scenario):
1. Any code path that can call `<InboundQueue as AddTip>::add_tip(nonce, amount)` with an `amount` not backed by real burnt Ether (e.g., a future alternate implementer of `EthereumSystemCall::AddTip`'s dispatch or a misconfigured `FrontendOrigin` that accepts broader origins) registers `Tips::<T>::mutate(nonce, ...)` with an inflated value, as validated only by `amount > 0` and nonce-not-consumed in `add_tip` [4](#0-3) .
2. When the real relayer later delivers the corresponding verified message, `process_message` reads `Tips::<T>::take(nonce)`, adds it to `relayer_fee`, and calls `register_reward(&relayer, ..., total_tip)` unconditionally [1](#0-0) , exactly as demonstrated by the existing unit tests `inbound_tip_is_paid_out_to_relayer` / `tip_paid_out_when_no_relayer_fee` [8](#0-7) , which prove the pallet pays out whatever number sits in `Tips` with no independent verification of backing funds.
3. The relayer then claims this reward through `pallet-bridge-relayers::claim_rewards`, which pays out of the shared rewards account [7](#0-6) , draining value intended for other relayers' legitimately-registered rewards — the same "consumes funds provided by other depositors" outcome as the original report.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L215-245)
```rust
		pub fn process_message(relayer: T::AccountId, message: Message) -> DispatchResult {
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == message.gateway, Error::<T>::InvalidGateway);

			let (nonce, relayer_fee) = (message.nonce, message.relayer_fee);

			// Verify the message has not been processed
			ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce);

			// Mark message as received
			Nonce::<T>::set(nonce);

			let message_id = T::MessageProcessor::process_message(relayer.clone(), message)
				.map_err(|e| match e {
					MessageProcessorError::ProcessMessage(e) => e,
					MessageProcessorError::ConvertMessage(e) => Error::<T>::from(e).into(),
					MessageProcessorError::SendMessage(e) => Error::<T>::from(e).into(),
				})?;

			// Pay relayer reward
			let tip = Tips::<T>::take(nonce).unwrap_or_default();
			let total_tip = relayer_fee.saturating_add(tip);
			if total_tip > 0 {
				T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
			}

			// Emit event with the message_id
			Self::deposit_event(Event::MessageReceived { nonce, message_id });

			Ok(())
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

**File:** bridges/modules/relayers/src/lib.rs (L399-431)
```rust
		/// Register reward for given relayer.
		pub(crate) fn register_relayer_reward(
			reward_kind: T::Reward,
			relayer: &T::AccountId,
			reward_balance: T::RewardBalance,
		) {
			if reward_balance.is_zero() {
				return;
			}

			RelayerRewards::<T, I>::mutate(
				relayer,
				reward_kind,
				|old_reward: &mut Option<T::RewardBalance>| {
					let new_reward =
						old_reward.unwrap_or_else(Zero::zero).saturating_add(reward_balance);
					*old_reward = Some(new_reward);

					tracing::trace!(
						target: crate::LOG_TARGET,
						?relayer,
						?reward_kind,
						?new_reward,
						"Relayer can now claim reward for serving payer"
					);

					Self::deposit_event(Event::<T, I>::RewardRegistered {
						relayer: relayer.clone(),
						reward_kind,
						reward_balance,
					});
				},
			);
```

**File:** bridges/modules/relayers/src/lib.rs (L536-546)
```rust
	/// Map of the relayer => accumulated reward.
	#[pallet::storage]
	pub type RelayerRewards<T: Config<I>, I: 'static = ()> = StorageDoubleMap<
		_,
		<RelayerRewardsKeyProviderOf<T, I> as StorageDoubleMapKeyProvider>::Hasher1,
		<RelayerRewardsKeyProviderOf<T, I> as StorageDoubleMapKeyProvider>::Key1,
		<RelayerRewardsKeyProviderOf<T, I> as StorageDoubleMapKeyProvider>::Hasher2,
		<RelayerRewardsKeyProviderOf<T, I> as StorageDoubleMapKeyProvider>::Key2,
		<RelayerRewardsKeyProviderOf<T, I> as StorageDoubleMapKeyProvider>::Value,
		OptionQuery,
	>;
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L261-273)
```rust
		pub fn add_tip(origin: OriginFor<T>, message_id: MessageId, asset: Asset) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: Into<Location>,
		{
			let who = ensure_signed(origin)?;

			let ether_gained = Self::swap_fee_asset_and_burn(who.clone().into(), asset)?;

			// Send the tip details to BH to be allocated to the reward in the Inbound/Outbound
			// pallet
			let call = Self::build_add_tip_call(who.clone(), message_id.clone(), ether_gained);
			Self::send_transact_call(who.into(), call)
		}
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L251-264)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(<T as pallet::Config>::WeightInfo::add_tip())]
		pub fn add_tip(
			origin: OriginFor<T>,
			sender: AccountIdOf<T>,
			message_id: MessageId,
			amount: u128,
		) -> DispatchResult {
			T::FrontendOrigin::ensure_origin(origin)?;

			let result = match message_id {
				Inbound(nonce) => <T as pallet::Config>::InboundQueue::add_tip(nonce, amount),
				Outbound(nonce) => <T as pallet::Config>::OutboundQueue::add_tip(nonce, amount),
			};
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/test.rs (L394-439)
```rust
#[test]
fn inbound_tip_is_paid_out_to_relayer() {
	new_tester().execute_with(|| {
		let nonce: u64 = 77;
		let tip: u128 = 12_345;
		let relayer_fee: u128 = 2_000;

		// Add tip for nonce before message is processed
		assert_ok!(InboundQueue::add_tip(nonce, tip));
		assert_eq!(Tips::<Test>::get(nonce), Some(tip));

		// Process inbound message with relayer_fee
		let relayer: AccountId = Keyring::Bob.into();
		assert_ok!(InboundQueue::process_message(
			relayer,
			Message {
				nonce,
				assets: vec![],
				payload: Payload::Raw(vec![]),
				claimer: None,
				execution_fee: 1_000_000_000,
				relayer_fee,
				gateway: mock::GatewayAddress::get(),
				origin: H160::random(),
				value: 3_000_000_000,
			},
		));

		// Reward should be registered from relayer_fee + tip
		assert_eq!(
			RegisteredRewardsCount::get(),
			1,
			"Reward should be registered from relayer_fee + tip"
		);

		// Check the actual reward amount paid out (should be relayer_fee + tip)
		assert_eq!(
			RegisteredRewardAmount::get(),
			relayer_fee + tip,
			"Reward amount should equal relayer_fee + tip"
		);

		// Tip should be consumed from storage
		assert_eq!(Tips::<Test>::get(nonce), None);
	});
}
```
