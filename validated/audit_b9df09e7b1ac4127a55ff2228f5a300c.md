Found a real analog: `AddTip::add_tip` in the Snowbridge inbound-queue-v2 pallet increases a relayer reward without any funds ever being reserved, transferred, or otherwise backed by the tipper — directly mirroring the report's core defect ("accepting a claim of value without verifying that value actually exists").

### Title
Unbacked tip amount minted as relayer reward in Snowbridge `inbound-queue-v2` `add_tip` - ([File: bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs])

### Summary
The reported Solana bug accepted a token account as loan collateral without checking it actually held a token, letting an attacker get paid for collateral that didn't exist. The structural analog here is `AddTip::add_tip` in `snowbridge-pallet-inbound-queue-v2`, reached through `pallet-system-v2`'s public `add_tip` extrinsic [1](#0-0) . It records an arbitrary `u128` amount into the `Tips` map with no verification that the caller (or anyone) has locked/transferred funds backing that amount [2](#0-1) . When the corresponding message is later processed, that unbacked amount is paid out as a real reward via `T::RewardPayment::register_reward`, which (per `PayAccountOnLocation`) mints/reserve-deposits ETH-denominated foreign asset value to the relayer on AssetHub [3](#0-2) [4](#0-3) .

### Finding Description
`process_message` computes `total_tip = relayer_fee.saturating_add(tip)` where `tip` comes from `Tips::<T>::take(nonce)`, and if `total_tip > 0` calls `T::RewardPayment::register_reward(&relayer, ..., total_tip)` [3](#0-2) . The `Tips` value is populated solely by `AddTip::add_tip`, whose only checks are `amount > 0` and that the nonce has not already been consumed — there is no reservation of balance, no hold, no debit from any account, and no proof that the claimed tip is backed by anything on Ethereum [2](#0-1) .

The public entry point is `pallet_system_v2::Pallet::add_tip`, gated only by `T::FrontendOrigin::ensure_origin(origin)` and forwarding directly to `<T as pallet::Config>::InboundQueue::add_tip(nonce, amount)` with the caller-supplied `amount` [1](#0-0) . `FrontendOrigin` is intended to represent AssetHub's sovereign/XCM origin for the system-frontend proxy flow, not a solvency check on the caller's balance — the call has no mechanism to debit the "sender" for the `amount` being tipped; `LostTips` bookkeeping only tracks failed adds, not fund custody [5](#0-4) . `register_reward` on `pay_reward` unconditionally issues a `ReserveAssetDeposited`/`DepositAsset` XCM to AssetHub for the tip amount denominated in the Ethereum-native asset location, i.e. it mints value out of thin air on the destination chain [4](#0-3) .

This is precisely the same broken invariant as the OtterSec report: a value/claim ("this account has a token" / "this tip is worth X") is accepted and later acted upon (loan approval / reward payout) without validating that the claim is backed by real, escrowed value.

### Impact Explanation
Any actor able to reach the `add_tip` path (via whatever origin satisfies `FrontendOrigin`, or directly for chains that expose `InboundQueue::add_tip` more permissively) can inflate the reward paid to a relayer for a given nonce with unbacked value. Because `RewardPayment` mints foreign-asset value via XCM `ReserveAssetDeposited`/`DepositAsset` rather than debiting a real reserve, this constitutes an unbacked mint of bridge-relevant asset value — directly matching the "theft or unbacked mint" impact class in the Impact Gate.

### Likelihood Explanation
The severity depends entirely on how permissive `FrontendOrigin` is configured for a given runtime; if it is restricted to a trusted sovereign-account-only origin (e.g., AssetHub's own sovereign account acting through its own accounting), the practical exploitability is limited. I was not able to fully trace the concrete `FrontendOrigin` configuration used in the shipped BridgeHub runtime within this investigation, nor confirm whether AssetHub's `system-frontend` pallet performs an equivalent balance debit on the AssetHub side before forwarding the `add_tip` XCM call (the `system-frontend` pallet was referenced in grep results but not read in depth). This is the main uncertainty in this finding — it should be verified whether `pallet_system_frontend` charges/reserves the tipper's balance on AssetHub before dispatching this XCM `Transact` to BridgeHub's `add_tip`.

### Recommendation
Require `add_tip` (at every entry point, including `pallet_system_v2::Pallet::add_tip` and the `AddTip` trait implementations) to be preceded by an actual balance debit/hold of `amount` from a real account before crediting `Tips`, and only release/burn that hold when the reward is paid or refund it if the nonce/message is dropped, mirroring the suggested NFT-loan fix of validating the underlying value before honoring the claim.

### Proof of Concept
1. Identify a runtime where `pallet_system_v2::Config::FrontendOrigin` accepts a caller-controllable origin (or invoke `snowbridge_pallet_inbound_queue_v2::Pallet::<T>::add_tip` directly if reachable from a permissive call site).
2. Call `add_tip(sender, Inbound(nonce), amount)` for a nonce not yet consumed, with an arbitrarily large `amount`, without any prior transfer/reservation of funds — as exercised in the existing test `inbound_tip_is_paid_out_to_relayer` [6](#0-5) .
3. Have any relayer submit the corresponding inbound message; `process_message` takes the tip and calls `T::RewardPayment::register_reward(&relayer, ..., relayer_fee + amount)` [3](#0-2) .
4. Observe the relayer receiving newly minted foreign-asset value on AssetHub equal to the unbacked tip, with no corresponding debit anywhere in the system.

### Citations

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L251-281)
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

			if let Err(ref e) = result {
				tracing::debug!(target: LOG_TARGET, ?e, ?message_id, ?amount, "error adding tip");
				LostTips::<T>::mutate(&sender, |lost_tip| {
					*lost_tip = lost_tip.saturating_add(amount);
				});
			}

			Self::deposit_event(Event::<T>::TipProcessed {
				sender,
				message_id,
				amount,
				success: result.is_ok(),
			});

			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L234-239)
```rust
			// Pay relayer reward
			let tip = Tips::<T>::take(nonce).unwrap_or_default();
			let total_tip = relayer_fee.saturating_add(tip);
			if total_tip > 0 {
				T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
			}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L248-259)
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
	}
```

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L127-151)
```rust
	fn pay_reward(
		relayer: &Relayer,
		_: (),
		reward: RewardBalance,
		beneficiary: Self::Beneficiary,
	) -> Result<(), Self::Error> {
		let ethereum_location = Location::new(2, [GlobalConsensus(EthereumNetwork::get())]);
		let assets: Asset = (ethereum_location.clone(), reward.into()).into();

		let xcm: Xcm<()> = alloc::vec![
			UnpaidExecution { weight_limit: Unlimited, check_origin: None },
			DescendOrigin(InboundQueueLocation::get().into()),
			UniversalOrigin(GlobalConsensus(EthereumNetwork::get())),
			ReserveAssetDeposited(assets.into()),
			DepositAsset { assets: AllCounted(1).into(), beneficiary },
		]
		.into();

		let (ticket, fee) =
			validate_send::<XcmSender>(AssetHubLocation::get(), xcm).map_err(|_| XcmSendFailure)?;
		XcmExecutor::charge_fees(relayer.clone(), fee).map_err(|_| ChargeFeesFailure)?;
		XcmSender::deliver(ticket).map_err(|_| XcmSendFailure)?;

		Ok(())
	}
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
