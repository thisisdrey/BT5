## Finding: Reward "claim" on Snowbridge/BridgeHub is settled on XCM *delivery*, not on actual *deposit* success

### Title
Bridge relayer rewards are marked as paid on XCM send/delivery success, not on actual asset deposit — permanent reward loss without recourse - (File: `bridges/snowbridge/primitives/core/src/reward.rs`)

### Summary
`PayAccountOnLocation::pay_reward`, used as the `PaymentProcedure` for Snowbridge relayer rewards on BridgeHub, only checks that an XCM program was *validated and delivered* to the outbound queue. It never checks whether the `ReserveAssetDeposited`/`DepositAsset` instructions inside that program actually *executed* successfully on Asset Hub. `pallet-bridge-relayers::do_claim_rewards` treats a `pay_reward() == Ok(())` as final settlement and irreversibly removes the relayer's `RelayerRewards` entry before the remote deposit is even attempted. This is the direct analog of the H-03 pattern: the "return value" of the real value-moving operation (the remote deposit) is never checked before the caller advances its own state as if the transfer had succeeded.

### Finding Description
`PayAccountOnLocation::pay_reward` builds an XCM program that descends origin to the inbound-queue pallet, sets `UniversalOrigin(GlobalConsensus(Ethereum))`, does `ReserveAssetDeposited`, then `DepositAsset { .. , beneficiary }`, and sends it to Asset Hub: [1](#0-0) 

The function's success criterion is only `validate_send`, `charge_fees`, and `XcmSender::deliver(ticket)` — i.e. "was the message accepted into the queue," not "did the deposit reach the beneficiary." There is no receipt/callback mechanism that reports the outcome of the remote `DepositAsset` back to BridgeHub.

Meanwhile, `pallet-bridge-relayers::do_claim_rewards` takes the reward out of storage and calls `pay_reward`; only a synchronous `Err` from `pay_reward` (XCM validate/charge/deliver failure) rolls the storage mutation back via `try_mutate_exists`. Any other outcome is treated as final: [2](#0-1) 

This is wired into BridgeHub Westend's reward payer exactly this way: [3](#0-2) 

The remote `DepositAsset` instruction is well known to fail for mundane reasons — beneficiary below `ExistentialDeposit`/`BelowMinimum`, unsupported/invalid beneficiary `Location`, frozen or non-existent asset account, etc. — and the xcm-executor's own test suite confirms that on `DepositAsset` failure the assets are *trapped*, not rolled back to the original owner or returned to the sender chain: [4](#0-3) 

Because the trap-triggering XCM program used `UniversalOrigin(GlobalConsensus(Ethereum))`, any trapped assets are registered under the Ethereum universal location as trap owner — not the relayer's account and not reachable through any process the relayer (or a normal user) controls. A recent related patch even confirms that small ("dust") deposit failures are silently burned rather than causing the whole program to fail-safe: [5](#0-4) 

The net effect: `pallet-bridge-relayers` advances its payout state (clears `RelayerRewards`, emits `RewardPaid`) purely on the strength of "the message was delivered," exactly analogous to trusting an ERC-20 `transfer()` call that returns without reverting but returns `false`/does nothing — the actual settlement result is never checked.

### Impact Explanation
Any relayer using `claim_rewards_to` with a `BridgeRewardBeneficiaries::AssetHubLocation` beneficiary that fails to receive the deposit on Asset Hub (fresh/underfunded account below ED, frozen account, unsupported junction, or any transient AssetHub-side condition) permanently loses their earned reward: the claim is deleted from `RelayerRewards` on BridgeHub, `RewardPaid` is emitted, yet no value ever reaches the beneficiary and the trapped assets are not recoverable by the relayer. This is a "permanent user-fund lock" — payout state advances before settlement is confirmed, violating the required invariant that payout state must only advance after execution and settlement succeed atomically.

### Likelihood Explanation
This does not require a malicious relayer, validator, governance actor, or leaked keys — it can be triggered by an ordinary relayer simply supplying a beneficiary account that is unfunded/below ED (a very common real-world condition for a "fresh" reward-claim destination), or by any transient failure at the Asset Hub side (congestion, asset restrictions, etc.). No privileged action is needed to reach the bug; the vulnerable code path (`pay_reward` → `do_claim_rewards`) is the sole, always-used settlement path for Snowbridge relayer rewards.

### Recommendation
Do not treat the local `pay_reward` call as final settlement for cross-chain reward payouts. Either:
- Require an explicit delivery/settlement receipt from Asset Hub (mirroring the `DeliveryReceipt` pattern already used elsewhere in Snowbridge V2, e.g. `process_delivery_receipt`) before permanently clearing `RelayerRewards`, or
- Keep the claim in a "pending" state until confirmed, with a fallback to re-credit `RelayerRewards` if the remote deposit is observed/reported as failed or trapped, or
- Change the beneficiary trap ownership so the relayer (not `GlobalConsensus(Ethereum)`) is the recorded trap origin, so relayers have a guaranteed on-chain path to reclaim trapped funds if delivery succeeds but deposit fails.

### Proof of Concept
1. A relayer accrues a reward via `register_reward(relayer, BridgeReward::Snowbridge, amount)`.
2. Relayer calls `claim_rewards_to(origin, BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(loc))` where `loc` resolves to an Asset Hub account with zero balance and `amount` below the relevant asset's `ExistentialDeposit`/min balance for the WETH/foreign-asset instance.
3. `do_claim_rewards` removes the entry from `RelayerRewards` and emits `RewardPaid` because `PayAccountOnLocation::pay_reward` returns `Ok(())` (XCM validated and delivered).
4. On Asset Hub, the `DepositAsset` instruction inside the delivered XCM fails with `BelowMinimum`/`NotAllowed`; per `polkadot/xcm/xcm-executor/src/tests/deposit_with_retry.rs`, the assets are trapped under the `GlobalConsensus(Ethereum)` trap key, not returned or credited to the relayer.
5. The relayer's `RelayerRewards` entry is gone (cannot re-claim: `NoRewardForRelayer`), and the relayer has no accessible mechanism to reclaim the trapped assets — the reward is permanently lost.

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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L117-137)
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
```

**File:** polkadot/xcm/xcm-executor/src/tests/deposit_with_retry.rs (L34-71)
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
```

**File:** prdoc/stable2503-4/pr_8351.prdoc (L1-10)
```text
title: "XCM: treat BelowMinimum deposit errors as non-fatal and burn non-depositable dust"
doc:
- audience: Runtime User
  description:
    Treat `TokenError::BelowMinimum` error as non-fatal in xcm-executor,
    ignore failures to deposit dust. Any dust that cannot be deposited,
    shall be dropped (effectively burned).
crates:
- name: staging-xcm-executor
  bump: patch
```
