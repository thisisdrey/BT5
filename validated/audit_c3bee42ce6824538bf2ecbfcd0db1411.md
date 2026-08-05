Audit Report

## Title
Reward settlement finalized on XCM send success, not remote mint success — permanent relayer-reward loss - (File: `bridges/snowbridge/primitives/core/src/reward.rs`)

## Summary
`PayAccountOnLocation::pay_reward`, the `PaymentProcedure` used for `BridgeReward::Snowbridge`, returns `Ok(())` once the reward-minting XCM to AssetHub has passed `validate_send`, `charge_fees`, and `deliver`, without any confirmation that the remote `ReserveAssetDeposited`/`DepositAsset` instructions actually execute on AssetHub. The caller, `do_claim_rewards`, treats this local `Ok(())` as final settlement, permanently deleting the relayer's `RelayerRewards` entry and emitting `RewardPaid` before (and regardless of) remote execution outcome.

## Finding Description
`pay_reward` constructs an `UnpaidExecution` XCM with `DescendOrigin`, `UniversalOrigin`, `ReserveAssetDeposited`, and `DepositAsset`, and considers the operation successful purely based on local delivery mechanics: [1](#0-0) . None of `validate_send`, `charge_fees`, or `deliver` verify anything about execution on the destination chain — they only confirm the message was accepted for delivery locally.

`do_claim_rewards` in the bridge relayers pallet consumes this result via `try_mutate_exists`, which only preserves the `RelayerRewards` entry if the closure returns `Err`. Since `pay_reward` returns `Ok` whenever the send succeeds, the closure calls `maybe_reward.take()` (removing the entry) and unconditionally emits `Event::RewardPaid` once `pay_reward` returns `Ok`: [2](#0-1) . There is no rollback or retry mechanism tied to remote settlement.

This contrasts with `PayRewardFromAccount`, the other `PaymentProcedure` implementation in the same pallet, which performs an atomic on-chain `transfer` with immediate, synchronously-verified settlement: [3](#0-2)  — i.e., for that path "success" genuinely means the beneficiary received funds, whereas for `PayAccountOnLocation` it only means "a message was queued."

The `beneficiary: Location` for `claim_rewards_to` is fully caller-controlled and only version-converted on BridgeHub before being forwarded into `pay_reward` — there is no chain-side validation that the location will successfully resolve into a valid, ED-satisfying account and that the `ether` foreign asset deposit will succeed on AssetHub: [4](#0-3) .

The existing unit tests for `pay_reward` only exercise the three local failure modes (`validate_send`, `charge_fees`, `deliver`) and never assert on remote-side settlement, confirming that no such check exists in the code path: [5](#0-4) .

## Impact Explanation
This violates the invariant that "payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." Because `RelayerRewards` is cleared and `RewardPaid` is emitted purely on local XCM send success, any remote-side failure (unregistered `ether` asset, bad beneficiary conversion, insufficient existential deposit, barrier rejection of the derived origin) results in a relayer's earned reward being permanently and irrecoverably erased from storage with no value delivered to any beneficiary. This is a fund-loss condition in the bridge reward-payout path.

## Likelihood Explanation
`claim_rewards_to` only requires `ensure_signed`, and the `beneficiary` `Location` is entirely relayer-supplied. Because AssetHub-side execution conditions (asset registration state, ED requirements, location-to-account conversion, barrier rules) are outside BridgeHub's control and outside the relayer's guaranteed control once the message is in flight, this can be triggered by ordinary usage — not just a crafted attack — and is fully reproducible given any mismatch between BridgeHub's assumptions and AssetHub's actual runtime state at execution time.

## Recommendation
Do not finalize (delete) the `RelayerRewards` entry or emit `RewardPaid` until remote settlement is confirmed via a receipt/acknowledgment mechanism (analogous to delivery-receipt handling in the outbound queue). Alternatively, retain the reward in an escrow/claimable state until confirmed success is reported, permitting the relayer to retry with a corrected beneficiary if the first XCM traps remotely; or validate the beneficiary `Location`/asset registration state synchronously on BridgeHub before consuming the local reward entry.

## Proof of Concept
1. Relayer accrues a `Snowbridge` reward via `register_reward`.
2. Relayer calls `claim_rewards_to` with a `BridgeRewardBeneficiaries::AssetHubLocation(location)` whose `location` fails to resolve to a valid, ED-satisfying AssetHub account (or otherwise causes `DepositAsset` to trap on AssetHub).
3. On BridgeHub: `validate_send`, `charge_fees`, and `deliver` all succeed, so `pay_reward` returns `Ok(())`, `RelayerRewards` entry is removed via `try_mutate_exists`, and `RewardPaid` is emitted per `bridges/modules/relayers/src/lib.rs` lines 263-301.
4. On AssetHub: the XCM executes but `DepositAsset` fails/traps — no `pallet_assets::Event::Deposited` is produced for the beneficiary, unlike the success path asserted in the emulated test at `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_rewards.rs` lines 90-102.
5. Relayer retries `claim_rewards_to` and receives `NoRewardForRelayer` — the reward is permanently lost with no funds delivered.

### Citations

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L145-150)
```rust
		let (ticket, fee) =
			validate_send::<XcmSender>(AssetHubLocation::get(), xcm).map_err(|_| XcmSendFailure)?;
		XcmExecutor::charge_fees(relayer.clone(), fee).map_err(|_| ChargeFeesFailure)?;
		XcmSender::deliver(ticket).map_err(|_| XcmSendFailure)?;

		Ok(())
```

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L246-266)
```rust
	#[test]
	fn pay_reward_success() {
		let relayer = MockRelayer(AccountId32::new([1u8; 32]));
		let beneficiary = Location::new(1, Here);
		let reward = 1_000u128;

		type TestedPayAccountOnLocation = PayAccountOnLocation<
			MockRelayer,
			u128,
			EthereumNetwork,
			AssetHubLocation,
			InboundQueueLocation,
			MockXcmSender,
			MockXcmExecutor,
			MockCall,
		>;

		let result = TestedPayAccountOnLocation::pay_reward(&relayer, (), reward, beneficiary);

		assert!(result.is_ok());
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

**File:** bridges/modules/relayers/src/payment_adapter.rs (L179-225)
```rust
	#[test]
	fn pay_reward_from_account_actually_pays_reward() {
		type Balances = pallet_balances::Pallet<TestRuntime>;
		type PayLaneRewardFromAccount =
			PayRewardFromAccount<Balances, ThisChainAccountId, TestLaneIdType, RewardBalance>;

		run_test(|| {
			let in_lane_0 = RewardsAccountParams::new(
				TestLaneIdType::try_new(1, 2).unwrap(),
				*b"test",
				RewardsAccountOwner::ThisChain,
			);
			let out_lane_1 = RewardsAccountParams::new(
				TestLaneIdType::try_new(1, 3).unwrap(),
				*b"test",
				RewardsAccountOwner::BridgedChain,
			);

			let in_lane0_rewards_account = PayLaneRewardFromAccount::rewards_account(in_lane_0);
			let out_lane1_rewards_account = PayLaneRewardFromAccount::rewards_account(out_lane_1);

			assert_ok!(Balances::mint_into(&in_lane0_rewards_account, 200));
			assert_ok!(Balances::mint_into(&out_lane1_rewards_account, 100));
			assert_eq!(Balances::balance(&in_lane0_rewards_account), 200);
			assert_eq!(Balances::balance(&out_lane1_rewards_account), 100);
			assert_eq!(Balances::balance(&1), 0);
			assert_eq!(Balances::balance(&2), 0);

			assert_ok!(PayLaneRewardFromAccount::pay_reward(&1, in_lane_0, 100, 1_u64));
			assert_eq!(Balances::balance(&in_lane0_rewards_account), 100);
			assert_eq!(Balances::balance(&out_lane1_rewards_account), 100);
			assert_eq!(Balances::balance(&1), 100);
			assert_eq!(Balances::balance(&2), 0);

			assert_ok!(PayLaneRewardFromAccount::pay_reward(&1, out_lane_1, 100, 1_u64));
			assert_eq!(Balances::balance(&in_lane0_rewards_account), 100);
			assert_eq!(Balances::balance(&out_lane1_rewards_account), 0);
			assert_eq!(Balances::balance(&1), 200);
			assert_eq!(Balances::balance(&2), 0);

			assert_ok!(PayLaneRewardFromAccount::pay_reward(&1, in_lane_0, 100, 2_u64));
			assert_eq!(Balances::balance(&in_lane0_rewards_account), 0);
			assert_eq!(Balances::balance(&out_lane1_rewards_account), 0);
			assert_eq!(Balances::balance(&1), 200);
			assert_eq!(Balances::balance(&2), 100);
		});
	}
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L117-136)
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
```
