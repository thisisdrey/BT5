Based on my research, I found a concrete local analog of the "premature state finalization" bug class described in the report — but instead of missing a state change (letting a redemption repeat), the polkadot-sdk analog is the inverse-but-equally-invalid pattern: the claim state is *finalized irreversibly* before the actual value transfer is confirmed to have succeeded, which can permanently destroy the claim without delivering funds.

### Title
Relayer reward claim is irreversibly consumed on XCM dispatch success, not on actual settlement, causing permanent loss of reward funds - ([File: bridges/modules/relayers/src/lib.rs])

### Summary
`Pallet::do_claim_rewards` in the bridge-relayers pallet removes the relayer's pending reward from storage as soon as `T::PaymentProcedure::pay_reward` returns `Ok(())`. For the Snowbridge `PaymentProcedure` (`PayAccountOnLocation::pay_reward`), "success" only means the XCM message asking AssetHub to mint/deposit the reward asset was successfully *handed to the XCM router* (`XcmSender::deliver` succeeded) — it does **not** confirm that the `ReserveAssetDeposited`/`DepositAsset` instructions actually executed and credited the beneficiary on AssetHub. Because the reward entry is deleted from `RelayerRewards` at the moment of dispatch rather than at confirmed settlement, any downstream execution failure on AssetHub (bad/degenerate beneficiary `Location`, trapped assets, existential-deposit failure, etc.) permanently destroys the relayer's claim with no way to retry or reclaim it.

### Finding Description
`do_claim_rewards` uses `RelayerRewards::<T, I>::try_mutate_exists` and calls `maybe_reward.take()` to remove the reward *before* calling `T::PaymentProcedure::pay_reward`: [1](#0-0) 

If `pay_reward` returns `Err`, the `try_mutate_exists` closure's `Err` causes the whole mutation (including the `take()`) to be reverted — this part is correct. The problem is that for the Snowbridge reward kind, `pay_reward` returns `Ok(())` far too early: [2](#0-1) 

The instructions sent are `UnpaidExecution`, `DescendOrigin`, `UniversalOrigin`, `ReserveAssetDeposited`, `DepositAsset` — i.e. an unpaid, best-effort XCM program with no error handler. `pay_reward` only validates and delivers this XCM to the outbound queue; it never observes whether AssetHub actually executed `DepositAsset` successfully. Since `XcmSender::deliver(ticket)` succeeding is sufficient for `Ok(())`, `do_claim_rewards` commits the removal of the reward and emits `RewardPaid` even though the beneficiary might never receive anything, e.g., if:
- the caller-supplied `BridgeRewardBeneficiaries::AssetHubLocation(VersionedLocation)` decodes to a `Location` that is syntactically valid but semantically un-depositable on AssetHub (unsupported junction combination for the configured `LocationToAccountId`), or
- the deposit amount is below AssetHub's existential deposit for the reward asset, or
- any other transient failure occurs in AssetHub's message queue execution of that specific XCM.

In all these cases the `ReserveAssetDeposited` assets end up trapped in AssetHub's holding register (`AssetsTrapped`) rather than credited to the beneficiary, while on BridgeHub the claim has already been irrevocably deleted and marked `RewardPaid`.

### Impact Explanation
This breaks the "settlement state must only advance after execution/settlement succeeds atomically" invariant. The relayer's accrued reward — real economic value already registered on-chain via `register_reward` — can be permanently destroyed by a single `claim_rewards_to` call whose remote leg fails, with no retry mechanism (the storage entry is gone) and no guaranteed path to reclaim the trapped assets on AssetHub (trap claiming there depends on origin/claimer set by the XCM program, which is derived from `DescendOrigin`/`UniversalOrigin` from Ethereum, not the relayer). This is a permanent user-fund loss, matching the "permanent user-fund or bridge-state lock" impact category.

### Likelihood Explanation
Any relayer that has accrued a `Snowbridge` reward can trigger this by calling the public, unprivileged `claim_rewards_to` extrinsic with an attacker/self-chosen `BridgeRewardBeneficiaries::AssetHubLocation` value. No privileged actor, relayer collusion, or malicious validator is required — only a normal signed account controlling its own claim (or one that mistakenly/maliciously constructs a bad beneficiary location), which is squarely an unprivileged public-entrypoint path.

### Recommendation
Do not remove the reward from `RelayerRewards` (or emit `RewardPaid`) until the payment is confirmed settled. For asynchronous, XCM-based payment procedures, either:
- keep the claim in a "pending/attempted" state (analogous to `pallet-treasury`'s `PaymentState::Attempted`/`check_status` pattern) until delivery/execution success is confirmed via a receipt or callback, and only clear it on confirmed success, restoring it to claimable on confirmed failure; or
- require `pay_reward` to only return `Ok` once the deposit is confirmed (e.g. by using a paid, error-handled XCM program with an explicit success acknowledgment) rather than treating "handed off to the router" as final settlement.

### Proof of Concept
1. Relayer accrues a `BridgeReward::Snowbridge` reward via `register_reward` (e.g. through `EthereumInboundQueueV2::process_message`), verified in `RelayerRewards`.
2. Relayer calls `claim_rewards_to(BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(loc))` where `loc` is a `VersionedLocation` that converts fine via `Location::try_from` but whose junctions AssetHub's `DepositAsset`/`LocationToAccountId` cannot resolve to a valid account (or is below AssetHub's ED for the ETH-derived asset).
3. On BridgeHub: `PayAccountOnLocation::pay_reward` validates and delivers the XCM successfully → `do_claim_rewards` commits `take()` on `RelayerRewards`, emits `RewardPaid`.
4. On AssetHub: the XCM's `DepositAsset` fails/traps; `pallet_xcm::Event::AssetsTrapped` fires instead of `pallet_assets::Event::Deposited` (contrast with the passing case shown in the emulated test asserting `Deposited`): [3](#0-2) 
5. The relayer's reward is now gone from `RelayerRewards` with no way to re-claim it, while the corresponding value was never delivered to any beneficiary account.

### Citations

**File:** bridges/modules/relayers/src/lib.rs (L263-291)
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_rewards.rs (L90-102)
```rust
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
