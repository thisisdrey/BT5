## Analysis

The external report's core broken invariant: **an accounting/state update is recorded as final (funds "settled") based only on a successful outbound action, without confirming the actual amount that arrives at the recipient.** For `StakingToken.sol`, `deposit`/`withdraw` record the *requested* `amount` as if it were the *actual* transferred amount, ignoring that the real transfer could yield less (or more).

The strongest local analog is in the Snowbridge relayer-reward claim path, where `pallet-bridge-relayers::do_claim_rewards` clears the pending reward and emits `RewardPaid` as soon as an XCM message is *handed off to the transport layer*, not once the reward is actually credited on the destination chain. [1](#0-0) 

### Title
Reward claims are marked paid and cleared from storage before the cross-chain deposit is confirmed, causing silent, unrecoverable fund loss for Snowbridge relayers - (File: `bridges/snowbridge/primitives/core/src/reward.rs`)

### Summary
`PayAccountOnLocation::pay_reward` (used as the `PaymentProcedure` for `BridgeReward::Snowbridge`) sends a one-way, unconfirmed XCM (`UnpaidExecution` + `ReserveAssetDeposited` + `DepositAsset`) to AssetHub and returns `Ok(())` the moment the message is handed to the XCM router (`XcmSender::deliver`) — well before the remote chain executes the `DepositAsset` instruction. `do_claim_rewards` treats this `Ok(())` as final settlement: it removes the relayer's `RelayerRewards` entry and emits `RewardPaid` in the same atomic call. [2](#0-1) [3](#0-2) 

### Finding Description
`do_claim_rewards` is structured to be safe *only* if `PaymentProcedure::pay_reward` returning `Ok` implies the beneficiary was actually paid:

```rust
RelayerRewards::<T, I>::try_mutate_exists(&relayer, reward_kind, |maybe_reward| -> DispatchResult {
    let reward_balance = maybe_reward.take().ok_or(Error::<T, I>::NoRewardForRelayer)?;
    T::PaymentProcedure::pay_reward(&relayer, reward_kind, reward_balance, beneficiary.clone())
        .map_err(|e| { ... Error::<T, I>::FailedToPayReward })?;
    Self::deposit_event(Event::<T, I>::RewardPaid { .. });
    Ok(())
})
``` [1](#0-0) 

For the local `PayRewardFromAccount` implementation this assumption holds: `T::transfer` (a `fungible::Mutate` call) either moves the balance or fails atomically. But for `PayAccountOnLocation` (Snowbridge), `pay_reward` only validates and dispatches an XCM:

```rust
let (ticket, fee) = validate_send::<XcmSender>(AssetHubLocation::get(), xcm).map_err(|_| XcmSendFailure)?;
XcmExecutor::charge_fees(relayer.clone(), fee).map_err(|_| ChargeFeesFailure)?;
XcmSender::deliver(ticket).map_err(|_| XcmSendFailure)?;
Ok(())
``` [4](#0-3) 

The XCM program itself is:
```rust
let xcm: Xcm<()> = alloc::vec![
    UnpaidExecution { weight_limit: Unlimited, check_origin: None },
    DescendOrigin(InboundQueueLocation::get().into()),
    UniversalOrigin(GlobalConsensus(EthereumNetwork::get())),
    ReserveAssetDeposited(assets.into()),
    DepositAsset { assets: AllCounted(1).into(), beneficiary },
]
.into();
``` [5](#0-4) 

There is no `ReportError`/`SetAppendix`/query-based confirmation attached to this program, so BridgeHub never learns whether `DepositAsset` succeeded on AssetHub. If the remote execution fails — e.g. the foreign Ethereum-asset isn't registered on AssetHub yet, the beneficiary location can't be resolved to an account, the deposit fails existential-deposit checks, or a filter/barrier rejects the trailing instructions — the funds are trapped/dropped on AssetHub with no error propagated back. Meanwhile, on BridgeHub, `RelayerRewards` has already been irreversibly cleared (`maybe_reward.take()` executed in the *same* successful `try_mutate_exists` call) and `RewardPaid` has already been emitted, since `XcmSender::deliver` returning `Ok` is all that's required to reach that code path.

This mirrors the FOT-token bug class exactly: the pallet's internal accounting ("reward has been paid") is derived from a proxy signal (successful hand-off / successful transfer call) instead of the actual, confirmed value received by the intended party, and there is no reconciliation or refund path.

### Impact Explanation
A relayer who successfully claims a Snowbridge reward can have their claim permanently and silently voided: `RelayerRewards` is cleared, so `claim_rewards`/`claim_rewards_to` cannot be retried (`NoRewardForRelayer`), yet no tokens are ever deposited into their AssetHub account if the remote XCM execution fails. This is a permanent, unrecoverable loss of relayer funds — matching the "permanent user-fund ... lock" and "duplicate settlement or payout" impact categories (here inverted: zero settlement instead of duplicate, but the same root cause of state advancing without confirmed, atomic execution).

### Likelihood Explanation
This requires no privileged actor, malicious relayer, validator, or governance action — only an ordinary condition where AssetHub-side execution of the forwarded XCM fails (e.g., foreign asset for the reward not yet created/registered on AssetHub, or beneficiary account/location edge cases). The existing test suite only exercises the happy path (`claim_rewards_works`) and the "delivery fails" path (`FailedToSend`/`FailedToPayReward` when `XcmSender::deliver` itself errors); it does not cover — and the code does not defend against — the case where delivery succeeds but remote *execution* fails.

### Recommendation
Do not treat `XcmSender::deliver` success as final settlement for `PayAccountOnLocation`. Either:
- Use a confirmed/query-based payment mechanism (e.g. XCM `QueryResponse`/`ReportError` callbacks, similar in spirit to `PayOverXcm` in `polkadot/xcm/xcm-builder/src/pay.rs`) and only clear `RelayerRewards` once a success response is received, or
- Keep a re-claimable/retry state (e.g. move the reward to a "pending confirmation" bucket rather than deleting it) until remote settlement is confirmed, with a reconciliation/refund mechanism if the remote execution traps the assets. [6](#0-5) 

### Proof of Concept
1. On BridgeHub Westend, a relayer accrues a `BridgeReward::Snowbridge` reward via `register_reward` (as in `snowbridge_v2_inbound.rs`), confirmed by `RewardRegistered`. [7](#0-6) 
2. Relayer calls `BridgeRelayers::claim_rewards_to(origin, BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(loc))`.
3. Inside `do_claim_rewards`, `RelayerRewards` entry is taken (removed) and `BridgeRewardPayer::pay_reward` → `PayAccountOnLocation::pay_reward` is invoked; it builds the `ReserveAssetDeposited`/`DepositAsset` XCM and calls `XcmSender::deliver`. [8](#0-7) 
4. As long as HRMP delivery to AssetHub succeeds, `pay_reward` returns `Ok(())`; `RewardPaid` is emitted and the reward entry is gone from `RelayerRewards` — regardless of what actually happens when the message executes on AssetHub.
5. If, on AssetHub, `DepositAsset` traps (e.g. asset for the reward not registered, or the beneficiary sub-location cannot be resolved into a valid account), the relayer receives nothing, and has no way to re-claim the already-cleared reward: `claim_rewards`/`claim_rewards_to` for that `reward_kind` now returns `Error::<T, I>::NoRewardForRelayer`. [9](#0-8)

### Citations

**File:** bridges/modules/relayers/src/lib.rs (L263-302)
```rust
		fn do_claim_rewards(
			relayer: T::AccountId,
			reward_kind: T::Reward,
			beneficiary: BeneficiaryOf<T, I>,
		) -> DispatchResult {
			RelayerRewards::<T, I>::try_mutate_exists(
				&relayer,
				reward_kind,
				|maybe_reward| -> DispatchResult {
					let reward_balance =
						maybe_reward.take().ok_or(Error::<T, I>::NoRewardForRelayer)?;
					T::PaymentProcedure::pay_reward(
						&relayer,
						reward_kind,
						reward_balance,
						beneficiary.clone(),
					)
					.map_err(|e| {
						tracing::error!(
							target: LOG_TARGET,
							error=?e,
							?relayer,
							?reward_kind,
							?reward_balance,
							?beneficiary,
							"Failed to pay rewards"
						);
						Error::<T, I>::FailedToPayReward
					})?;

					Self::deposit_event(Event::<T, I>::RewardPaid {
						relayer: relayer.clone(),
						reward_kind,
						reward_balance,
						beneficiary,
					});
					Ok(())
				},
			)
		}
```

**File:** bridges/modules/relayers/src/lib.rs (L737-771)
```rust
	#[test]
	fn relayer_can_claim_reward_to() {
		run_test(|| {
			get_ready_for_events();

			RelayerRewards::<TestRuntime>::insert(
				REGULAR_RELAYER,
				test_reward_account_param(),
				100,
			);
			assert_ok!(Pallet::<TestRuntime>::claim_rewards_to(
				RuntimeOrigin::signed(REGULAR_RELAYER),
				test_reward_account_param(),
				REGULAR_RELAYER2,
			));
			assert_eq!(
				RelayerRewards::<TestRuntime>::get(REGULAR_RELAYER, test_reward_account_param()),
				None
			);

			// Check if the `RewardPaid` event was emitted.
			assert_eq!(
				System::<TestRuntime>::events().last(),
				Some(&EventRecord {
					phase: Phase::Initialization,
					event: TestEvent::BridgeRelayers(Event::RewardPaid {
						relayer: REGULAR_RELAYER,
						reward_kind: test_reward_account_param(),
						reward_balance: 100,
						beneficiary: REGULAR_RELAYER2,
					}),
					topics: vec![],
				}),
			);
		});
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

**File:** polkadot/xcm/xcm-builder/src/pay.rs (L29-77)
```rust
/// for XCM-based payments of a given `Balance` of some asset ID existing on some chain under
/// ownership of some `Interior` location of the local chain to a particular `Beneficiary`. The
/// `AssetKind` value is not itself bounded (to avoid the issue of needing to wrap some preexisting
/// datatype), however a converter type `AssetKindToLocatableAsset` must be provided in order to
/// translate it into a `LocatableAsset`, which comprises both an XCM `Location` describing
/// the XCM endpoint on which the asset to be paid resides and an XCM `AssetId` to identify the
/// specific asset at that endpoint.
///
/// This relies on the XCM `TransferAsset` instruction. A trait `BeneficiaryRefToLocation` must be
/// provided in order to convert the `Beneficiary` reference into a location usable by
/// `TransferAsset`.
///
/// `PayOverXcm::pay` is asynchronous, and returns a `QueryId` which can then be used in
/// `check_payment` to check the status of the XCM transaction.
///
/// See also `PayAccountId32OverXcm` which is similar to this except that `BeneficiaryRefToLocation`
/// need not be supplied and `Beneficiary` must implement `Into<[u8; 32]>`.
///
/// The implementation of this type assumes:
///
/// - The sending account on the remote chain is fixed (derived from the `Interior` location),
///   rather than being fully configurable.
/// - The remote chain waives the XCM execution fee (`PaysRemoteFee::No`).
///
/// See also [super::transfer::TransferOverXcm] for a more generic implementation with a flexible
/// sender account on the remote chain, and not making the assumption that the remote XCM execution
/// fee is waived.
pub type PayOverXcm<
	Interior,
	XcmConfig,
	Querier,
	Timeout,
	Beneficiary,
	AssetKind,
	AssetKindToLocatableAsset,
	BeneficiaryRefToLocation,
> = PayOverXcmWithHelper<
	Interior,
	TransferOverXcmHelper<
		XcmConfig,
		Querier,
		Timeout,
		Beneficiary,
		AssetKind,
		AssetKindToLocatableAsset,
		BeneficiaryRefToLocation,
	>,
>;

```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L620-633)
```rust
		EthereumInboundQueueV2::process_message(relayer_account.clone(), message).unwrap();

		assert_expected_events!(
			BridgeHubWestend,
			vec![
				RuntimeEvent::XcmpQueue(cumulus_pallet_xcmp_queue::Event::XcmpMessageSent { .. }) => {},
				// Check that the relayer reward was registered.
				RuntimeEvent::BridgeRelayers(pallet_bridge_relayers::Event::RewardRegistered { relayer, reward_kind, reward_balance }) => {
					relayer: *relayer == relayer_account,
					reward_kind: *reward_kind == BridgeReward::Snowbridge,
					reward_balance: *reward_balance == relayer_reward,
				},
			]
		);
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L117-135)
```rust
			BridgeReward::Snowbridge => {
				match beneficiary {
					BridgeRewardBeneficiaries::LocalAccount(_) => Err(Self::Error::Other("`LocalAccount` beneficiary is not supported for `Snowbridge` rewards!")),
					BridgeRewardBeneficiaries::AssetHubLocation(account_location) => {
						let account_location = Location::try_from(account_location)
							.map_err(|_| Self::Error::Other("`AssetHubLocation` beneficiary location version is not supported for `Snowbridge` rewards!"))?;
						snowbridge_core::reward::PayAccountOnLocation::<
							AccountId,
							u128,
							EthereumNetwork,
							AssetHubLocation,
							InboundQueueV2Location,
							XcmRouter,
							XcmExecutor<XcmConfig>,
							RuntimeCall
						>::pay_reward(
							relayer, (), reward, account_location
						)
					}
```
