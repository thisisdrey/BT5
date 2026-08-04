### Title
Relayer tips lost forever with no recovery path when `add_tip` fails after nonce consumption - (`File: bridges/snowbridge/pallets/system-v2/src/lib.rs`)

### Summary
`pallet-bridge-relayers` and the Snowbridge reward-tip mechanism accumulate relayer incentives that are only released via an explicit claim call. The Snowbridge System V2 `add_tip` extrinsic accepts a tip for an in-flight inbound/outbound message and forwards it into the target queue's reward accounting. If the underlying queue rejects the tip (most commonly because the message nonce was already consumed by the time the tip arrives — an ordinary race between message delivery and the tip-add transaction, not any malicious actor), the pallet does not revert or return the value to the sender. Instead it records the amount into a `LostTips` map and explicitly documents that this storage exists only to "support implementing a recovery method in the future" — i.e., no such recovery/claim call exists in the codebase today. This is the same broken invariant as the Concur report: value that was set aside for a specific beneficiary becomes permanently orphaned once the corresponding target (sheltered pool / already-processed nonce) can no longer receive it, and there is no sweep/claim mechanism to make the owner whole.

### Finding Description
`add_tip` in `snowbridge-pallet-system-v2` is the public entrypoint (reachable from AssetHub via `FrontendOrigin`/XCM) that lets a user top up a relayer reward for a specific `MessageId` (inbound or outbound nonce): [1](#0-0) 

The dispatch to the queue's `AddTip::add_tip` can fail with `AddTipError::NonceConsumed` (among other reasons) if the message has already been processed: [2](#0-1) 

When that happens, the pallet does not return an error to the caller (the call itself still returns `Ok(())`) and does not refund/return the tip value to the sender through any transfer — it only records the lost amount in the `LostTips` storage map, with the comment making clear that no recovery mechanism currently exists: [3](#0-2) [4](#0-3) 

The test suite confirms the outcome is silent bookkeeping of a stranded value rather than a refund or revert: [5](#0-4) 

This is structurally identical to the Concur bug: (1) a value is committed/allocated toward a specific target (a message nonce / a sheltered pool); (2) that target becomes permanently unable to receive it (nonce already consumed / pool sheltered); (3) the protocol keeps a bookkeeping record of the stranded value (`LostTips` / continued `MasterChef` allocation) but provides no extrinsic or sweep mechanism to return it to the rightful owner. In both cases the root cause is a structural gap (missing recovery/sweep call), not an admin or governance action — any ordinary user calling `add_tip` on a message that gets processed slightly before the tip lands triggers the loss.

### Impact Explanation
Value paid by a user (via `add_tip`, which is fronted by real cross-chain transfers from AssetHub, as exercised by `add_tip_from_asset_hub_user_origin`) is permanently and irrecoverably lost from the depositor's perspective once the race condition occurs, with no path in the current pallet API to claim it back. This matches the "permanent user-fund ... lock" acceptance criterion: funds are locked/unrecoverable due to a missing sweep/claim mechanism, not because of any privileged or malicious action. [6](#0-5) 

### Likelihood Explanation
This requires no malicious peer, relayer, validator, or admin action — it is triggered by ordinary operational timing: a relayer successfully delivers/finalizes a message (consuming its nonce) around the same time a user submits `add_tip` for that same nonce from AssetHub. Given asynchronous cross-consensus messaging (XCM from AssetHub to BridgeHub) and independent relaying of the underlying message, this race is a normal, foreseeable occurrence, not an edge case requiring privileged access.

### Recommendation
- When `T::InboundQueue::add_tip` / `T::OutboundQueue::add_tip` fails, refund the tip back to `sender` (via a transfer/mint) instead of only bookkeeping it in `LostTips`.
- Alternatively, implement the "recovery method" alluded to in the code comment: add a permissionless `claim_lost_tip` extrinsic that lets the `sender` withdraw their recorded `LostTips` balance.
- Ensure the frontend (`snowbridge-pallet-system-frontend`) only irrevocably transfers/burns the tip asset after `add_tip` succeeds on the backend, or holds it in an account that is provably swept by the recovery mechanism above.

### Proof of Concept
1. A user (relayer or generic actor) initiates a tip for inbound message nonce `N` via `SnowbridgeSystemFrontend::add_tip` on AssetHub, transferring value (e.g., DOT) as shown in `add_tip_from_asset_hub_user_origin`.
2. Concurrently, an independent relayer submits and finalizes the inbound message for nonce `N`, causing `NonceBitmap`/`Nonce::<T>::set(nonce)` to mark it consumed before the tip's XCM arrives on BridgeHub.
3. The forwarded `EthereumSystemV2::add_tip(origin, sender, MessageId::Inbound(N), amount)` call executes; `InboundQueue::add_tip` returns `AddTipError::NonceConsumed` as unit-tested in `add_tip_inbound_fails_when_nonce_is_consumed`.
4. The pallet does not revert or refund; it moves `amount` into `LostTips::<T>::get(sender)` and emits `TipProcessed { success: false, .. }`, per: [5](#0-4) 
5. There is no extrinsic in `snowbridge-pallet-system-v2` (or elsewhere in the reviewed code) that reads `LostTips` and pays the sender back — the value the user already paid remains permanently stranded.

### Citations

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L136-142)
```rust
	/// Relayer reward tips that were paid by the user to incentivize the processing of their
	/// message, but then could not be added to their message reward (e.g. the nonce was already
	/// processed or their order could not be found). Capturing the lost tips here supports
	/// implementing a recovery method in the future.
	#[pallet::storage]
	pub type LostTips<T: Config> =
		StorageMap<_, Blake2_128Concat, AccountIdOf<T>, u128, ValueQuery>;
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L251-271)
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

**File:** bridges/snowbridge/pallets/system-v2/src/tests.rs (L197-219)
```rust
#[test]
fn add_tip_inbound_fails_when_nonce_is_consumed() {
	new_test_ext(true).execute_with(|| {
		let origin = make_xcm_origin(FrontendLocation::get());
		let sender: AccountId = Keyring::Alice.into();
		// In `MockOkInboundQueue`, the mocked implementation returns an error when the nonce is
		// equal to 3, to simulate an error condition.
		let message_id = MessageId::Inbound(FAILING_NONCE);
		let amount = 1000;

		assert_ok!(EthereumSystemV2::add_tip(origin, sender.clone(), message_id.clone(), amount));

		System::assert_last_event(RuntimeEvent::EthereumSystemV2(Event::<Test>::TipProcessed {
			sender: sender.clone(),
			message_id,
			amount,
			success: false,
		}));

		let lost_tip = LostTips::<Test>::get(sender);
		assert_eq!(lost_tip, 1000);
	});
}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L1366-1401)
```rust
#[test]
pub fn add_tip_from_asset_hub_user_origin() {
	fund_on_bh();
	register_assets_on_ah();
	fund_on_ah();
	set_up_eth_and_dot_pool();
	let relayer = AssetHubWestendSender::get();

	// Add the tip to a nonce that has not been processed.
	let tip_message_id = MessageId::Inbound(2);

	let dot = Location::new(1, Here);
	AssetHubWestend::execute_with(|| {
		type RuntimeOrigin = <AssetHubWestend as Chain>::RuntimeOrigin;

		assert_ok!(<AssetHubWestend as AssetHubWestendPallet>::SnowbridgeSystemFrontend::add_tip(
			RuntimeOrigin::signed(relayer.clone()),
			tip_message_id.clone(),
			xcm::prelude::Asset::from((dot, 1_000_000_000u128)),
		));
	});

	BridgeHubWestend::execute_with(|| {
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;

		let events = BridgeHubWestend::events();
		assert!(
			events.iter().any(|event| matches!(
				event,
				RuntimeEvent::EthereumSystemV2(snowbridge_pallet_system_v2::Event::TipProcessed { sender, message_id, success, ..})
					if *sender == relayer &&*message_id == tip_message_id.clone() && *success, // expect success
			)),
			"tip added event found"
		);
	});
}
```
