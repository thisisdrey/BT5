Audit Report

## Title
`claim_rewards_to`/`claim_rewards` permanently clear `RelayerRewards` on successful XCM *send* alone, before AssetHub actually executes `DepositAsset`, causing irrecoverable loss of a relayer's Snowbridge reward when remote settlement fails - (File: bridges/snowbridge/primitives/core/src/reward.rs, bridges/modules/relayers/src/lib.rs)

## Summary
`PayAccountOnLocation::pay_reward` in `bridges/snowbridge/primitives/core/src/reward.rs` returns `Ok(())` once the reward-payout XCM is validated, fees are charged, and the message is handed to `XcmSender::deliver` — none of which guarantees that AssetHub subsequently executes `DepositAsset` and credits the beneficiary. `do_claim_rewards` in `bridges/modules/relayers/src/lib.rs` treats this local `Ok` as final and permanently deletes the relayer's `RelayerRewards` entry via `try_mutate_exists`/`maybe_reward.take()` before any remote confirmation exists. If remote execution later fails (beneficiary below ED for the foreign asset, unregistered asset, weight-limit issues), the WETH/foreign-asset reward never lands with the intended beneficiary while `RelayerRewards` has already been zeroed on Bridge Hub with no re-credit path.

## Finding Description
`pay_reward` builds an XCM (`UnpaidExecution`, `DescendOrigin`, `UniversalOrigin`, `ReserveAssetDeposited`, `DepositAsset { .. , beneficiary }`) and only checks the outcome of validate/charge_fees/deliver: [1](#0-0) 

`Ok(())` here reflects successful enqueueing for delivery, not remote execution success. Back on Bridge Hub: [2](#0-1) 

`maybe_reward.take()` unconditionally removes the storage entry inside the `try_mutate_exists` closure, and since `pay_reward` returning `Ok` is the only gate, the removal commits as soon as the send succeeds — well before AssetHub processes the `DepositAsset`. Notably, the XCM built in `pay_reward` contains no `SetHints { AssetClaimer }`; if `DepositAsset` fails and the assets are trapped, they get trapped against the derived bridge-origin context (per the `AssetsTrapped`/`claim_assets` tests in `cumulus/parachains/integration-tests/.../snowbridge_v2_inbound.rs`), not against the relayer's chosen beneficiary — so the relayer generally has no direct path to reclaim the trapped value even manually. The existing unit test in `cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/tests/tests.rs` (lines 787-795) confirms the pallet only guards against *send*-time failure (XCM not deliverable due to missing HRMP channel), returning `FailedToPayReward` and leaving storage untouched — it does **not** cover the remote-execution-fails-after-successful-send case, which is exactly the gap exploited here.

## Impact Explanation
This breaks the required invariant that "payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." The relayer's claimable `RelayerRewards` balance — an accounting value backing real WETH reward funds — is destroyed on Bridge Hub with no guaranteed, relayer-reachable credit on AssetHub, constituting a permanent, unbacked loss of relayer funds with no compensating or re-credit mechanism.

## Likelihood Explanation
Any relayer can trigger this via the public `claim_rewards_to`/`claim_rewards` extrinsics. The failure condition (beneficiary account below existential deposit for the foreign asset, unregistered/unreachable asset registration state, or other remote `DepositAsset` failure) is a plausible, non-malicious production condition, not requiring any privileged actor, compromised relayer, or off-chain infrastructure control — it only requires normal signed usage combined with an adverse but reachable remote execution outcome.

## Recommendation
Do not clear `RelayerRewards` based on successful XCM send alone. Either require a confirmed remote execution/settlement signal from AssetHub before finalizing the claim, or keep the reward entry pending and re-credit it back into `RelayerRewards` if remote execution fails or an `AssetsTrapped` event is observed for that claim's context. Additionally, consider setting an explicit `AssetClaimer` hint targeting the beneficiary in the payout XCM so that, at minimum, failed deposits can be manually reclaimed by the intended recipient rather than trapped against an unreachable bridge-derived origin.

## Proof of Concept
1. Relayer accrues a `BridgeReward::Snowbridge` balance via `register_reward`.
2. Relayer calls `claim_rewards_to` with an `AssetHubLocation` beneficiary that is new/below ED for the WETH foreign asset, or with conditions causing `DepositAsset` to fail remotely.
3. `PayAccountOnLocation::pay_reward` succeeds through `validate_send`, `charge_fees`, and `XcmSender::deliver`, returning `Ok(())`.
4. `do_claim_rewards`'s `try_mutate_exists` commits, deleting the `RelayerRewards` entry and emitting `RewardPaid`.
5. On AssetHub, `DepositAsset` fails and the reserve-deposited assets are trapped against the bridge's derived origin (as demonstrated by `fallback_claimer_traps_to_bridge_owner_and_claim_assets_succeeds` in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs`), not the relayer's chosen beneficiary.
6. Result: `RelayerRewards` is permanently zero on Bridge Hub and the relayer has no direct claim on the trapped WETH on AssetHub.

### Citations

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L136-151)
```rust
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
