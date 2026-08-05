Audit Report

## Title
Snowbridge relayer reward is irreversibly deleted from `RelayerRewards` before the cross-chain XCM payment is confirmed, permanently destroying the reward if remote execution on AssetHub fails - (File: `bridges/modules/relayers/src/lib.rs`, `bridges/snowbridge/primitives/core/src/reward.rs`)

## Summary
`Pallet::do_claim_rewards` removes the relayer's `RelayerRewards` entry via `try_mutate_exists`/`take()` and emits `RewardPaid` as soon as `T::PaymentProcedure::pay_reward` returns `Ok(())`, but for `BridgeReward::Snowbridge` that `Ok(())` only means the `ReserveAssetDeposited`/`DepositAsset` XCM was validated, fee-charged, and handed to `XcmSender::deliver` — not that AssetHub actually executed the deposit. [1](#0-0) [2](#0-1) 

## Finding Description
`PayAccountOnLocation::pay_reward` constructs an `UnpaidExecution`-gated XCM that performs `ReserveAssetDeposited` + `DepositAsset` on AssetHub and returns `Ok(())` immediately after `validate_send`, `charge_fees`, and `XcmSender::deliver` succeed. [3](#0-2)  None of these three calls provide any signal about whether the destination chain (AssetHub) actually executes the deposit instructions — `deliver` only confirms the message was accepted into the transport queue.

`do_claim_rewards` treats that local `Ok(())` as final settlement: it uses `try_mutate_exists` with `maybe_reward.take()`, which unconditionally removes the `RelayerRewards` storage entry for `(relayer, reward_kind)` before calling `pay_reward`, and only re-inserts nothing back on success — there's no re-credit path if the remote deposit later fails. [4](#0-3)  Once `pay_reward` returns `Ok`, `RewardPaid` is emitted and the entry is gone, regardless of whether AssetHub's XCM executor actually credits the beneficiary.

This breaks the invariant that "message queues... and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" — here the local ledger settles unconditionally on delivery-queuing success, not on remote execution success. There is no receipt, callback, or reconciliation mechanism in this module to detect a failed remote execution and restore the `RelayerRewards` entry.

## Impact Explanation
If the `ReserveAssetDeposited`/`DepositAsset` XCM fails to execute on AssetHub for any reason not visible to the sender (e.g., transient destination-side rejection, asset-registration/filter state), the relayer's reward is permanently destroyed on BridgeHub: the `RelayerRewards` entry has already been `take()`n, `RewardPaid` has already been emitted, and no extrinsic or storage exists to reclaim or re-register the lost entitlement. This matches the "permanent user-fund lock/loss" impact category, since the relayer performed real work and is left with no recourse, without any privileged/governance action needed to cause it.

## Likelihood Explanation
The `claim_rewards`/`claim_rewards_to` extrinsics are permissionless and routinely called by relayers. [5](#0-4)  The vulnerable pattern (local storage cleared on delivery-queuing success, not on remote execution confirmation) is unconditionally present on every call — it does not require a malicious peer, validator, or governance action to exist, only a scenario where the destination-side execution diverges from the sender's assumption of success, which the code has no way to detect or recover from.

## Recommendation
- Do not treat `XcmSender::deliver` success as final settlement for the `RelayerRewards` entry. Defer removal of the ledger entry until a confirmed execution receipt from AssetHub is received, similar to relay-proof-gated settlement patterns elsewhere in the bridge stack.
- Add a reconciliation/retry path: if the AssetHub-side deposit fails, re-credit `RelayerRewards` on BridgeHub so the relayer can re-claim.
- Consider replacing the best-effort `UnpaidExecution` design with a paid/verifiable XCM path so failures at the destination are observable and actionable by BridgeHub.

## Proof of Concept
1. Relayer accrues a `BridgeReward::Snowbridge` entry via `register_reward`.
2. Relayer calls `claim_rewards_to(BridgeReward::Snowbridge, beneficiary)`, invoking `do_claim_rewards`, which calls `RelayerRewards::take()` to remove the entry before calling `PaymentProcedure::pay_reward`. [6](#0-5) 
3. `PayAccountOnLocation::pay_reward` validates, charges fees, and calls `XcmSender::deliver(ticket)`, returning `Ok(())` purely based on local delivery success. [3](#0-2) 
4. `RewardPaid` is emitted and the `RelayerRewards` entry is permanently gone.
5. If the `ReserveAssetDeposited`/`DepositAsset` instructions fail to execute on AssetHub, no funds land at the beneficiary, and no on-chain mechanism exists to detect this or restore the relayer's reward entitlement.

### Citations

**File:** bridges/modules/relayers/src/lib.rs (L224-235)
```rust
		/// Claim accumulated rewards and send them to the alternative beneficiary.
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::claim_rewards_to())]
		pub fn claim_rewards_to(
			origin: OriginFor<T>,
			reward_kind: T::Reward,
			beneficiary: BeneficiaryOf<T, I>,
		) -> DispatchResult {
			let relayer = ensure_signed(origin)?;

			Self::do_claim_rewards(relayer, reward_kind, beneficiary)
		}
```

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
