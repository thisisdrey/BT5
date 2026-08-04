## Analysis

The external report's core broken invariant is: **an irreversible state-advancing action (marking redemption as settled) depends on a single external step succeeding, with no confirmation of actual completion, and no fallback/rescue path if that step silently fails** — leading to a permanent fund lock.

I found a direct structural analog in the Snowbridge reward-claim flow inside `pallet-bridge-relayers`.

### Title
Snowbridge relayer reward payout advances to "paid" state on XCM dispatch acceptance rather than confirmed execution, permanently losing rewards on destination-side deposit failure - (File: `bridges/modules/relayers/src/lib.rs`, `bridges/snowbridge/primitives/core/src/reward.rs`)

### Summary
`pallet-bridge-relayers::do_claim_rewards` clears the relayer's accumulated reward and emits `RewardPaid` as soon as `T::PaymentProcedure::pay_reward` returns `Ok(())`. For the Snowbridge reward kind, `pay_reward` is implemented by `PayAccountOnLocation::pay_reward`, which only validates and *enqueues* (`XcmSender::deliver`) an XCM to AssetHub — it never confirms that the destination-side `DepositAsset` into the caller-supplied beneficiary actually succeeds. If that deposit fails on AssetHub (e.g., unknown/unregistered asset for the beneficiary, beneficiary below existential deposit, or any other execution failure), the reserved assets are silently trapped in AssetHub's holding register while BridgeHub has already deleted the reward record and emitted the success event. The reward is gone with no recovery path available to the relayer.

### Finding Description
`do_claim_rewards` in `bridges/modules/relayers/src/lib.rs` operates as follows: [1](#0-0) 

The reward is taken out of storage, `pay_reward` is invoked, and only reverted if `pay_reward` returns an `Err`. Crucially, `pay_reward` for `BridgeReward::Snowbridge` is `PayAccountOnLocation::pay_reward`: [2](#0-1) 

This function builds an `UnpaidExecution` XCM containing `ReserveAssetDeposited` + `DepositAsset { beneficiary, .. }`, calls `validate_send`, charges local fees, and calls `XcmSender::deliver(ticket)`. It returns `Ok(())` the moment the message is **accepted into the outbound queue** — it does not wait for, or receive any confirmation of, the XCM's execution on AssetHub. From BridgeHub's perspective, "delivery accepted" is treated as "reward paid": the `RelayerRewards` storage entry is deleted and `Event::RewardPaid` is emitted before the destination side has done anything.

If the `beneficiary` (an XCM `Location` chosen by the relayer itself via `claim_rewards_to`) does not deposit-check successfully on AssetHub — for example an unrecognized location, an account that can't receive the reserve asset, or one below the existential deposit — `DepositAsset` fails and the assets remaining in the holding register are trapped (`AssetsTrapped`) under the complex composed origin `DescendOrigin(InboundQueueLocation) + UniversalOrigin(GlobalConsensus(EthereumNetwork))`. No ordinary signed account can reproduce that origin to call `pallet_xcm::claim_assets`, so the funds become permanently unclaimable — mirroring exactly the class of bug the codebase already fixed once for the *inbound message* asset-claimer path (see `prdoc/stable2603-3/pr_11919.prdoc`, which patched a different-but-analogous "fallback claimer trap location mismatch → unrecoverable funds" issue), but this fix was never applied to the reward-payment path.

### Impact Explanation
This breaks the required invariant that "payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." Marking a reward as `RewardPaid` and deleting the reward ledger entry based purely on successful XCM *dispatch* (not *execution*) means a relayer's legitimately earned reward can be permanently destroyed with no ability to retry (the ledger entry is gone) and generally no ability to recover the trapped assets (the trap origin is not reproducible by any account able to sign a claim). This is a genuine, permanent user-fund loss for an unprivileged actor (the relayer), consistent with the "permanent user-fund ... lock" and "duplicate settlement" impact categories in the gate.

### Likelihood Explanation
The trigger is entirely reachable by an ordinary, unprivileged relayer calling the public `claim_rewards_to` extrinsic with a `BridgeRewardBeneficiaries::AssetHubLocation` value that fails deposit-checks on AssetHub (malformed/unregistered/dust location) — no admin, governance, or malicious relayer/validator assumption is required. It does not require an adversarial party; it can happen from a relayer's own configuration mistake, or via routine failure modes (asset not yet registered for a beneficiary, ED not met, momentary AssetHub-side issue), and once triggered it is irreversible since the ledger entry is already erased on BridgeHub.

### Recommendation
Do not clear `RelayerRewards` / emit `RewardPaid` on BridgeHub until execution success on AssetHub is confirmed (e.g., via a receipt/ack mechanism), or make the reward-claim flow reversible by only removing the ledger entry once a corresponding execution confirmation is received; alternatively route Snowbridge reward payouts through a pattern equivalent to the one used to fix the inbound-queue asset-claim trap (`pr_11919`), ensuring any trapped assets are automatically claimable by a location that maps back to the relayer.

### Proof of Concept
1. Relayer accrues a Snowbridge reward via `register_reward` (from normal inbound/outbound queue processing).
2. Relayer calls `pallet_bridge_relayers::claim_rewards_to(origin, BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(some_location))` where `some_location` resolves to an account/asset combination that will fail AssetHub's `DepositAsset` check (e.g., unregistered foreign asset for that account, or account below ED).
3. On BridgeHub: `pay_reward` → `validate_send` + `deliver` succeed (ticket accepted) → `do_claim_rewards` treats this as success, deletes the `RelayerRewards` entry, emits `RewardPaid`.
4. On AssetHub: the XCM executes, `DepositAsset` fails, remaining holding assets are trapped under an origin the relayer cannot reproduce with a signed claim.
5. Relayer has no reward record left on BridgeHub (cannot retry) and no accessible claim path on AssetHub — funds are permanently lost, matching the report's "immutable single path → no fallback → permanent lock" bug class. [1](#0-0) [2](#0-1)

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
