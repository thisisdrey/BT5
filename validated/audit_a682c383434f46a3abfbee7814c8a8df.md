Audit Report

## Title
Snowbridge relayer reward claim clears local reward ledger on XCM *send* success, not on destination-chain settlement, causing permanent, unrecoverable loss of claimed rewards - (File: `bridges/snowbridge/primitives/core/src/reward.rs`)

## Summary
`pallet-bridge-relayers::claim_rewards_to` on BridgeHub finalizes ("takes") the relayer's `RelayerRewards` storage entry and treats the reward as fully paid as soon as `PaymentProcedure::pay_reward` returns `Ok(())`. For the Snowbridge reward kind, `pay_reward` is implemented by `PayAccountOnLocation::pay_reward`, which returns `Ok(())` immediately after the XCM instructing AssetHub to `ReserveAssetDeposited` + `DepositAsset` has only been validated, fee-charged, and handed to the transport layer for delivery — not after the deposit actually executes and lands on AssetHub.

## Finding Description
`do_claim_rewards` uses `RelayerRewards::<T, I>::try_mutate_exists` and calls `maybe_reward.take()` to remove the balance from storage before invoking `T::PaymentProcedure::pay_reward`; only if `pay_reward` returns `Err` does the storage mutation roll back, and on `Ok` the removal and `Event::RewardPaid` are committed atomically [1](#0-0) . For `BridgeReward::Snowbridge`, the payment procedure wired into the runtime is `snowbridge_core::reward::PayAccountOnLocation` [2](#0-1) . Its `pay_reward` builds an XCM performing `ReserveAssetDeposited` + `DepositAsset` targeting AssetHub, but returns `Ok(())` purely based on `validate_send`, `XcmExecutor::charge_fees`, and `XcmSender::deliver` succeeding — none of which confirm the destination-chain `DepositAsset` execution outcome [3](#0-2) .

The `bp_relayers::PaymentProcedure` trait signature used here is a simple synchronous `Result<(), Error>` with no receipt/query mechanism, unlike the codebase's own `frame_support::traits::tokens::Pay` trait, which explicitly separates `pay()` (returns an async `Id`) from `check_payment(id)` for later status verification [4](#0-3) . This split exists precisely because XCM payments are asynchronous, and other payment paths in the repo (e.g. `PayOverXcm`/`TransferOverXcmHelper`) implement it correctly: `send_remote_transfer_xcm` returns a `QueryId`, and `check_transfer` later inspects the query response to distinguish `Success`/`Failure`/`InProgress` [5](#0-4) . `PayAccountOnLocation` has no equivalent — it discards the delivery hash and never checks or records any downstream confirmation, so `do_claim_rewards` has no way to detect or react to remote execution failure and the `RelayerRewards` entry is already gone.

The integration test `claim_rewards_works` confirms this exact flow: `RewardPaid` fires on BridgeHub and the reward is cleared as soon as `pay_reward` succeeds, with the actual AssetHub-side `Deposited` event only checked afterward as a separate, unlinked assertion [6](#0-5) . If that later AssetHub execution fails instead of succeeding (e.g., asset filter mismatch, beneficiary below ED, version mismatch), there is no rollback path — a subsequent `claim_rewards_to` call would fail with `NoRewardForRelayer` because the entry was already permanently removed.

## Impact Explanation
This is a permanent, unrecoverable loss of a relayer's earned reward: the payout state (`RelayerRewards` removal + `RewardPaid` event) advances before the cross-chain settlement (`DepositAsset` execution on AssetHub) is confirmed to succeed, violating the required invariant that payout state must only advance after execution and settlement succeed atomically. This matches the "permanent user-fund or bridge-state lock" impact category, since the relayer has no in-protocol path to reclaim the reward once the local ledger entry is cleared while the actual asset deposit either fails or gets trapped on AssetHub.

## Likelihood Explanation
No malicious actor is required — an ordinary relayer calling the public `claim_rewards_to` extrinsic with a beneficiary location that AssetHub cannot successfully deposit into (bad/expired `VersionedLocation`, below-ED beneficiary, asset registration/filter issues) will trigger permanent loss of the reward, since `validate_send`/`charge_fees`/`deliver` on BridgeHub can succeed while the actual `DepositAsset` execution on AssetHub fails asynchronously in a later block. The repo's own `claim_snowbridge_rewards_to_local_account_fails` test demonstrates how easily beneficiary-variant issues surface, though that specific case is caught synchronously (returns `FailedToPayReward`) — the vulnerable window is instead the class of failures that occur *after* successful local send but *before/at* remote execution, which the pallet has no mechanism to detect.

## Recommendation
Do not clear/finalize `RelayerRewards` (or emit `RewardPaid`) synchronously with local `pay_reward` success for asynchronous cross-chain procedures. Adopt a receipt/query-based confirmation pattern analogous to `PayOverXcm`'s `QueryId`/`check_payment` (or `TransferOverXcmHelper::check_transfer`) so the local claim state is only cleared once destination-chain execution success is confirmed, or keep the reward in a "claim in flight" state with a path to restore it if remote execution is later observed to fail (e.g., via `AssetsTrapped` correlation or a settlement callback).

## Proof of Concept
1. Register a Snowbridge reward for `relayer_account` via `register_reward` on BridgeHub, as done in `claim_rewards_works`.
2. Call `claim_rewards_to` with a `BridgeRewardBeneficiaries::AssetHubLocation(...)` that passes `validate_send`/`charge_fees`/`deliver` on BridgeHub but whose `DepositAsset` fails to execute on AssetHub (e.g., beneficiary account below ED for the reward asset, or asset filter mismatch) — same structural setup as `claim_rewards_works` but with the AssetHub-side deposit made to fail. [7](#0-6) 
3. Observe on BridgeHub: `Event::RewardPaid` is emitted and `RelayerRewards::get(relayer, BridgeReward::Snowbridge)` returns `None`.
4. Observe on AssetHub: no `pallet_assets::Event::Deposited` for the beneficiary; instead trapped assets or a failed message-queue processing event.
5. Attempt `claim_rewards_to` again on BridgeHub: it returns `Error::NoRewardForRelayer`, confirming the relayer cannot recover the reward despite no successful settlement. [8](#0-7)

### Citations

**File:** bridges/modules/relayers/src/lib.rs (L263-301)
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
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L117-139)
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
				}
			}
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

**File:** substrate/frame/support/src/traits/tokens/pay.rs (L44-53)
```rust
	/// Make a payment and return an identifier for later evaluation of success in some off-chain
	/// mechanism (likely an event, but possibly not on this chain).
	fn pay(
		who: &Self::Beneficiary,
		asset_kind: Self::AssetKind,
		amount: Self::Balance,
	) -> Result<Self::Id, Self::Error>;
	/// Check how a payment has proceeded. `id` must have been previously returned by `pay` for
	/// the result of this call to be meaningful.
	fn check_payment(id: Self::Id) -> PaymentStatus;
```

**File:** polkadot/xcm/xcm-builder/src/transfer.rs (L231-250)
```rust
		let (ticket, delivery_fees) =
			XcmConfig::XcmSender::validate(&mut Some(asset_location), &mut Some(message))?;
		xcm_executor::XcmExecutor::<XcmConfig>::charge_fees(from_location, delivery_fees)?;
		XcmConfig::XcmSender::deliver(ticket)?;

		Ok(query_id)
	}

	fn check_transfer(id: Self::QueryId) -> TransferStatus {
		use QueryResponseStatus::*;
		match Querier::take_response(id) {
			Ready { response, .. } => match response {
				Response::ExecutionResult(None) => TransferStatus::Success,
				Response::ExecutionResult(Some(_)) => TransferStatus::Failure,
				_ => TransferStatus::Unknown,
			},
			Pending { .. } => TransferStatus::InProgress,
			NotFound | UnexpectedVersion => TransferStatus::Unknown,
		}
	}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_rewards.rs (L63-102)
```rust
		let relayer_location = Location::new(
			0,
			[Junction::AccountId32 { id: reward_address.clone().into(), network: None }],
		);
		let reward_beneficiary =
			BridgeRewardBeneficiaries::AssetHubLocation(VersionedLocation::V5(relayer_location));
		let result = BridgeRelayers::claim_rewards_to(
			RuntimeOrigin::signed(relayer_account.clone()),
			BridgeReward::Snowbridge,
			reward_beneficiary.clone(),
		);
		assert_ok!(result);

		assert_expected_events!(
			BridgeHubWestend,
			vec![
				// Check that the pay reward event was emitted on BH
				RuntimeEvent::BridgeRelayers(pallet_bridge_relayers::Event::RewardPaid { relayer, reward_kind, reward_balance, beneficiary }) => {
					relayer: *relayer == relayer_account,
					reward_kind: *reward_kind == BridgeReward::Snowbridge,
					reward_balance: *reward_balance == reward_amount,
					beneficiary: *beneficiary == reward_beneficiary,
				},
			]
		);
	});

	AssetHubWestend::execute_with(|| {
		type RuntimeEvent = <AssetHubWestend as Chain>::RuntimeEvent;
		assert_expected_events!(
			AssetHubWestend,
			vec![
				// Check that the reward was paid on AH
				RuntimeEvent::ForeignAssets(pallet_assets::Event::Deposited { asset_id, who: owner, .. }) => {
					asset_id: *asset_id == eth_location(),
					owner: *owner == reward_address.clone().into(),
				},
			]
		);
	})
```
