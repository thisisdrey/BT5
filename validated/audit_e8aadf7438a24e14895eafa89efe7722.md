All cited code matches the repository exactly: `PayAccountOnLocation::pay_reward` builds an `UnpaidExecution`/`DescendOrigin`/`UniversalOrigin`/`ReserveAssetDeposited`/`DepositAsset` XCM and returns `Ok(())` purely on `validate_send` + `charge_fees` + `XcmSender::deliver` succeeding [1](#0-0) , and `do_claim_rewards` removes the `RelayerRewards` entry and emits `RewardPaid` as soon as `pay_reward` returns `Ok`, with no mechanism to re-credit on remote failure [2](#0-1) . `claim_rewards_to` is a plain signed extrinsic where any relayer can supply an arbitrary `beneficiary: BeneficiaryOf<T, I>` [3](#0-2) , wired for Snowbridge to `PayAccountOnLocation` with an `AssetHubLocation` beneficiary on BridgeHub Westend [4](#0-3) . The xcm-executor unit tests confirm that a failed `DepositAsset` (e.g., below-ED beneficiary) results in the assets being trapped via `AssetTrap`, not returned or credited to any accessible party [5](#0-4) .

This confirms the finding is technically accurate as described: the reward's settlement state (`RelayerRewards` removal, `RewardPaid` event) advances irreversibly based solely on local XCM delivery success, with no check of the remote `DepositAsset` outcome, no receipt mechanism, and no recovery path if the deposit fails. This matches the "payout state must only advance after execution and settlement succeed atomically" invariant violation called out in the Required Impacts / Pivots section, and the failure is reachable by an ordinary unprivileged relayer supplying a valid-but-underfunded/unsupported beneficiary location — no malicious node, validator, or leaked-key assumption is needed.

Audit Report

## Title
Bridge relayer rewards are marked as paid on XCM send/delivery success, not on actual asset deposit — permanent reward loss without recourse - (File: `bridges/snowbridge/primitives/core/src/reward.rs`)

## Summary
`PayAccountOnLocation::pay_reward`, the `PaymentProcedure` used for Snowbridge relayer rewards on BridgeHub, only verifies that an XCM program was validated and delivered to Asset Hub's outbound queue; it never confirms that the embedded `DepositAsset` instruction actually succeeded there. `pallet-bridge-relayers::do_claim_rewards` treats `pay_reward() == Ok(())` as final settlement, permanently deleting the relayer's `RelayerRewards` entry before the remote deposit outcome is known.

## Finding Description
`PayAccountOnLocation::pay_reward` constructs an XCM program (`DescendOrigin` → `UniversalOrigin(GlobalConsensus(Ethereum))` → `ReserveAssetDeposited` → `DepositAsset { beneficiary }`) and considers the call successful once `validate_send`, `charge_fees`, and `XcmSender::deliver` succeed [1](#0-0) . There is no delivery receipt or callback that reports whether `DepositAsset` executed on Asset Hub. `do_claim_rewards` removes the reward from `RelayerRewards` storage and only rolls back on a synchronous `Err` from `pay_reward`; any XCM program that is merely delivered but whose `DepositAsset` later fails on Asset Hub is treated as fully settled, and `RewardPaid` is emitted regardless [2](#0-1) . This procedure is the exact settlement path wired for `BridgeReward::Snowbridge` with an `AssetHubLocation` beneficiary on BridgeHub Westend [6](#0-5) . The `claim_rewards_to` extrinsic accepts an attacker/relayer-supplied beneficiary with no validation of ED-compliance or account existence [3](#0-2) . The xcm-executor's own test suite demonstrates that a failed `DepositAsset` (e.g., a sub-ED deposit) causes the leftover holding to be trapped via `AssetTrap`, not returned to sender or credited to the intended beneficiary [5](#0-4) ; because the trap occurs under a program that executed `UniversalOrigin(GlobalConsensus(Ethereum))`, the trapped assets are keyed to the Ethereum universal origin — not to the relayer's account.

## Impact Explanation
This constitutes a permanent user-fund lock: a relayer's rightfully earned reward is deleted from `RelayerRewards` and marked `RewardPaid` in state, yet no asset reaches the beneficiary, and the relayer has no accessible mechanism to recover the trapped value. This matches the "permanent user-fund or bridge-state lock" and "payout state must only advance after execution and settlement succeed atomically" impact categories.

## Likelihood Explanation
No privileged actor, malicious node/validator, or leaked key is required. Any ordinary signed relayer calling `claim_rewards_to` with a fresh/underfunded Asset Hub beneficiary location (a common, unremarkable condition) or hitting a transient Asset-Hub-side deposit failure will trigger this loss. The vulnerable path (`pay_reward` → `do_claim_rewards`) is the sole settlement path for Snowbridge relayer rewards, making this fully reproducible and repeatable.

## Recommendation
Do not treat local XCM delivery as final settlement for cross-chain payouts. Options: require an explicit settlement/delivery receipt from Asset Hub before permanently clearing `RelayerRewards`; keep claims in a "pending" state with re-credit on observed failure/trap; or change trap ownership so the relayer (not `GlobalConsensus(Ethereum)`) is the recorded trap origin, giving relayers a guaranteed reclaim path.

## Proof of Concept
1. Relayer accrues a reward via `register_reward(relayer, BridgeReward::Snowbridge, amount)`.
2. Relayer calls `claim_rewards_to(origin, BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(loc))` with `loc` resolving to a zero-balance Asset Hub account and `amount` below the relevant asset's minimum balance.
3. `do_claim_rewards` deletes the `RelayerRewards` entry and emits `RewardPaid` because `pay_reward` returns `Ok(())` (XCM validated and delivered) [7](#0-6) .
4. On Asset Hub, `DepositAsset` fails (e.g., `BelowMinimum`); per `polkadot/xcm/xcm-executor/src/tests/deposit_with_retry.rs`, the assets are trapped, not credited or returned [8](#0-7) .
5. The relayer's reward entry is gone (`NoRewardForRelayer` on re-claim attempt), and the relayer has no path to recover the trapped assets — the reward is permanently lost.

### Citations

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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L90-140)
```rust
pub struct BridgeRewardPayer;
impl bp_relayers::PaymentProcedure<AccountId, BridgeReward, u128> for BridgeRewardPayer {
	type Error = sp_runtime::DispatchError;
	type Beneficiary = BridgeRewardBeneficiaries;

	fn pay_reward(
		relayer: &AccountId,
		reward_kind: BridgeReward,
		reward: u128,
		beneficiary: BridgeRewardBeneficiaries,
	) -> Result<(), Self::Error> {
		match reward_kind {
			BridgeReward::RococoWestend(lane_params) => {
				match beneficiary {
					BridgeRewardBeneficiaries::LocalAccount(account) => {
						bp_relayers::PayRewardFromAccount::<
							Balances,
							AccountId,
							LegacyLaneId,
							u128,
						>::pay_reward(
							&relayer, lane_params, reward, account,
						)
					},
					BridgeRewardBeneficiaries::AssetHubLocation(_) => Err(Self::Error::Other("`AssetHubLocation` beneficiary is not supported for `RococoWestend` rewards!")),
				}
			},
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
}
```

**File:** polkadot/xcm/xcm-executor/src/tests/deposit_with_retry.rs (L34-72)
```rust
/// A single sub-ED deposit fails, the instruction is aborted, and the leftover holding is
/// trapped by `post_process` — funds are not lost.
#[test]
fn failed_deposit_aborts_instruction_and_post_process_traps_holding() {
	add_asset(SENDER, (Here, 1u128)); // 1 < ExistentialDeposit (=2 in mock)

	let xcm = Xcm::<TestCall>::builder_unsafe()
		.withdraw_asset((Here, 1u128))
		.deposit_asset(All, RECIPIENT)
		.build();

	let (mut vm, weight) = instantiate_executor(SENDER, xcm.clone());

	// `bench_process` returns `Err` because the retry-pass deposit failure now bubbles up.
	let result = vm.bench_process(xcm);
	let err = result.expect_err("retry-pass deposit failure must bubble up");

	// Mirror what `XcmExecutor::execute` does between `process` and `post_process`: register
	// the instruction error so `post_process` produces `Outcome::Incomplete`.
	vm.set_error(Some((err.index, err.xcm_error)));

	let outcome = vm.bench_post_process(weight);
	assert!(
		matches!(outcome, Outcome::Incomplete { .. }),
		"expected Outcome::Incomplete, got {outcome:?}"
	);

	// Recipient never received anything.
	assert!(asset_list(RECIPIENT).is_empty());

	// `post_process` trapped the holding (which `transactional_process` had restored after
	// the failed `DepositAsset`). The mock `TestAssetTrap` accumulates everything under
	// `TRAPPED_ASSETS`.
	assert_eq!(
		asset_list(TRAPPED_ASSETS),
		vec![(Here, 1u128).into()],
		"undeposited assets must be trapped, not silently lost"
	);
}
```
