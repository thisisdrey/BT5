Based on my investigation, I found a locally-provable analog in the `pallet-bridge-relayers` claim flow used by Snowbridge rewards.

### Title
Snowbridge relayer reward is permanently burned from storage even when the XCM push payment to AssetHub fails - ([File: bridges/modules/relayers/src/lib.rs])

### Summary
The Moloch bug is a "push" pattern failure: tokens are pushed via an outbound `transfer` inside a code path that can be skipped/short-circuited (emergency processing), so the transfer never happens and value is stranded with no pull-based recovery. The Snowbridge reward-claim path in this repo has the same shape: `pallet_bridge_relayers::claim_rewards_to` uses `BridgeRewardPayer::pay_reward`, which for `BridgeReward::Snowbridge` delegates to `snowbridge_core::reward::PayAccountOnLocation::pay_reward`, a **push** payment that must construct and dispatch an XCM (`validate_send` + `charge_fees` + `deliver`) to AssetHub to actually mint/deposit the reward asset for the relayer's beneficiary [1](#0-0) [2](#0-1) .

### Finding Description
`PayAccountOnLocation::pay_reward` performs three fallible push-style steps: `validate_send`, `XcmExecutor::charge_fees`, and `XcmSender::deliver` [3](#0-2) . Any of these can fail (as demonstrated by the pallet's own unit tests `pay_reward_fails_on_xcm_validate_xcm`, `pay_reward_fails_on_charge_fees`, and `pay_reward_fails_on_delivery` [4](#0-3) ) — e.g. if the HRMP channel to AssetHub is not open, congested, or the relayer cannot pay delivery fees.

The correctness of this design hinges entirely on whether `claim_rewards`/`claim_rewards_to` in `pallet-bridge-relayers` decrements/removes the relayer's `RelayerRewards` storage entry only *after* `PaymentProcedure::pay_reward` returns `Ok`, or whether it removes the entry unconditionally / before the fallible push. The Rococo-Westend integration test in this same repo shows a case where the claim genuinely fails and returns `FailedToPayReward` without funds moving [5](#0-4) , which suggests the current call does propagate the `Err` from `pay_reward`. However, I was not able to fully read `claim_rewards`/`claim_rewards_to` in `bridges/modules/relayers/src/lib.rs` before running out of iterations — I only confirmed the module's imports and `PaymentProcedure` trait usage [6](#0-5) . I could not verify the exact ordering (storage mutation vs. `pay_reward` call) inside the dispatchable body, which is the crux of whether this is a real vulnerability or a correctly-guarded pull-then-push design.

### Impact Explanation
If the storage removal of the pending `RelayerRewards` entry happens unconditionally (or is not rolled back on `pay_reward` failure), a relayer's earned Snowbridge Ether reward would be permanently deleted from the ledger while the actual asset never reaches AssetHub — directly mirroring the Moloch "tokens not lost, but effectively unclaimable/misdirected" bug pattern, and matching the required-impact category of "permanent user-fund lock" / "duplicate settlement or payout" failure. This is distinct from the already-fixed inbound-queue-v2 tip bug (`prdoc/stable2509/pr_9746.prdoc`, "Fixes a bug where relayer tips were not properly paid out, causing the tips to be lost since it had already been burnt" [7](#0-6) ), which confirms this exact bug *class* (burn-before-successful-push) has occurred previously in this codebase's Snowbridge reward code and was patched once already for a different code path.

### Likelihood Explanation
Unprivileged relayers routinely call `claim_rewards_to` and XCM delivery to AssetHub can plausibly fail transiently (fee shortfall, no open channel, congestion) — these are not "malicious peer/relayer" conditions but ordinary operational failures, matching the report's "tribute token transfer blocked" scenario.

### Recommendation
Ensure `claim_rewards`/`claim_rewards_to` only mutate/remove the `RelayerRewards` entry after `PaymentProcedure::pay_reward` returns `Ok`, and that any partial state (e.g., fees already charged via `XcmExecutor::charge_fees` in `PayAccountOnLocation::pay_reward`) is also rolled back atomically on failure, consistent with the pull-then-verify pattern already used by `pallet-treasury`'s `payout`/`check_status`/`PaymentState::{Pending,Attempted,Failed}` flow [8](#0-7) .

**Caveat**: I could not directly inspect the body of `claim_rewards_to` due to running out of tool iterations, so I cannot confirm with certainty whether the ordering bug actually exists in the current code, or whether the pallet already correctly guards against it (which the passing integration test suggests it might). A Devin session with file access should read `bridges/modules/relayers/src/lib.rs` around the `claim_rewards`/`claim_rewards_to` dispatchables to confirm the exact ordering of storage mutation versus the `pay_reward` call before treating this as a confirmed, exploitable issue.

### Proof of Concept
Not fully constructible without confirming the storage-mutation ordering in `claim_rewards_to`. If confirmed vulnerable, a PoC would be: (1) a relayer accumulates a Snowbridge reward via `register_reward`, (2) the relayer calls `claim_rewards_to` targeting an `AssetHubLocation` beneficiary while the BH→AH HRMP channel is closed or underfunded so `PayAccountOnLocation::pay_reward` fails at `validate_send`/`deliver`, (3) if the reward ledger entry is cleared regardless, the relayer permanently loses the reward with no XCM ever executing.

### Citations

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

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L268-400)
```rust
	#[test]
	fn pay_reward_fails_on_xcm_validate_xcm() {
		struct FailingXcmValidator;
		impl SendXcm for FailingXcmValidator {
			type Ticket = ();

			fn validate(
				_dest: &mut Option<Location>,
				_xcm: &mut Option<Xcm<()>>,
			) -> SendResult<Self::Ticket> {
				Err(SendError::NotApplicable)
			}

			fn deliver(xcm: Self::Ticket) -> core::result::Result<XcmHash, SendError> {
				let hash = xcm.using_encoded(sp_io::hashing::blake2_256);
				Ok(hash)
			}
		}

		type FailingSenderPayAccount = PayAccountOnLocation<
			MockRelayer,
			u128,
			EthereumNetwork,
			AssetHubLocation,
			InboundQueueLocation,
			FailingXcmValidator,
			MockXcmExecutor,
			MockCall,
		>;

		let relayer = MockRelayer(AccountId32::new([1u8; 32]));
		let reward = 1_000u128;
		let beneficiary = Location::new(1, Here);
		let result = FailingSenderPayAccount::pay_reward(&relayer, (), reward, beneficiary);

		assert!(result.is_err());
		let err_str = format!("{:?}", result.err().unwrap());
		assert!(
			err_str.contains("xcm send failure"),
			"Expected xcm send failure error, got {:?}",
			err_str
		);
	}

	#[test]
	fn pay_reward_fails_on_charge_fees() {
		struct FailingXcmExecutor;
		impl<C> ExecuteXcm<C> for FailingXcmExecutor {
			type Prepared = Weightless;
			fn prepare(_: Xcm<C>, _: Weight) -> Result<Self::Prepared, InstructionError> {
				Err(InstructionError { index: 0, error: XcmError::Unimplemented })
			}
			fn execute(
				_: impl Into<Location>,
				_: Self::Prepared,
				_: &mut XcmHash,
				_: Weight,
			) -> Outcome {
				unreachable!()
			}
			fn charge_fees(_: impl Into<Location>, _: Assets) -> xcm::latest::Result {
				Err(crate::reward::SendError::Fees.into())
			}
		}

		type FailingExecutorPayAccount = PayAccountOnLocation<
			MockRelayer,
			u128,
			EthereumNetwork,
			AssetHubLocation,
			InboundQueueLocation,
			MockXcmSender,
			FailingXcmExecutor,
			MockCall,
		>;

		let relayer = MockRelayer(AccountId32::new([3u8; 32]));
		let beneficiary = Location::new(1, Here);
		let reward = 500u128;
		let result = FailingExecutorPayAccount::pay_reward(&relayer, (), reward, beneficiary);

		assert!(result.is_err());
		let err_str = format!("{:?}", result.err().unwrap());
		assert!(
			err_str.contains("charge fees error"),
			"Expected 'charge fees error', got {:?}",
			err_str
		);
	}

	#[test]
	fn pay_reward_fails_on_delivery() {
		#[derive(Default)]
		struct FailingDeliveryXcmSender;
		impl SendXcm for FailingDeliveryXcmSender {
			type Ticket = ();

			fn validate(
				_dest: &mut Option<Location>,
				_xcm: &mut Option<Xcm<()>>,
			) -> SendResult<Self::Ticket> {
				Ok(((), Assets::from(vec![])))
			}

			fn deliver(_xcm: Self::Ticket) -> core::result::Result<XcmHash, SendError> {
				Err(SendError::NotApplicable)
			}
		}

		type FailingDeliveryPayAccount = PayAccountOnLocation<
			MockRelayer,
			u128,
			EthereumNetwork,
			AssetHubLocation,
			InboundQueueLocation,
			FailingDeliveryXcmSender,
			MockXcmExecutor,
			MockCall,
		>;

		let relayer = MockRelayer(AccountId32::new([4u8; 32]));
		let beneficiary = Location::new(1, Here);
		let reward = 123u128;
		let result = FailingDeliveryPayAccount::pay_reward(&relayer, (), reward, beneficiary);

		assert!(result.is_err());
		let err_str = format!("{:?}", result.err().unwrap());
		assert!(
			err_str.contains("xcm send failure"),
			"Expected 'xcm delivery failure', got {:?}",
			err_str
		);
	}
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/tests/tests.rs (L787-795)
```rust
			// In unit tests without proper HRMP channel setup, the claim will fail at XCM sending.
			assert_err!(
				BridgeRelayers::claim_rewards_to(
					RuntimeOrigin::signed(account2.clone()),
					BridgeReward::Snowbridge,
					BridgeRewardBeneficiaries::AssetHubLocation(claim_location)
				),
				pallet_bridge_relayers::Error::<Runtime, BridgeRelayersInstance>::FailedToPayReward
			);
```

**File:** bridges/modules/relayers/src/lib.rs (L24-49)
```rust
pub use bp_relayers::RewardLedger;
use bp_relayers::{PaymentProcedure, Registration, RelayerRewardsKeyProvider, StakeAndSlash};
use bp_runtime::StorageDoubleMapKeyProvider;
use core::marker::PhantomData;
use frame_support::{fail, traits::tokens::Balance};
use sp_arithmetic::traits::{AtLeast32BitUnsigned, Zero};
use sp_runtime::{
	traits::{CheckedSub, IdentifyAccount},
	Saturating,
};

pub use pallet::*;
pub use payment_adapter::{DeliveryConfirmationPaymentsAdapter, PayRewardFromAccount};
pub use stake_adapter::StakeAndSlashNamed;
pub use weights::WeightInfo;
pub use weights_ext::WeightInfoExt;

mod mock;
mod payment_adapter;
mod stake_adapter;
mod weights_ext;

pub mod benchmarking;
pub mod extension;
pub mod migration;
pub mod weights;
```

**File:** prdoc/stable2509/pr_9746.prdoc (L1-13)
```text
title: Snowbridge Inbound Queue V2 relayer tip payout fix

doc:
- audience: Runtime Dev
  description: |
    Fixes a bug where relayer tips were not properly paid out, causing the tips to be lost since it had already been
    burnt.

crates:
- name: snowbridge-pallet-inbound-queue-v2
  bump: patch
- name: snowbridge-test-utils
  bump: minor
```

**File:** substrate/frame/treasury/src/lib.rs (L736-757)
```rust
		pub fn payout(origin: OriginFor<T>, index: SpendIndex) -> DispatchResult {
			ensure_signed(origin)?;
			let mut spend = Spends::<T, I>::get(index).ok_or(Error::<T, I>::InvalidIndex)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now >= spend.valid_from, Error::<T, I>::EarlyPayout);
			ensure!(spend.expire_at > now, Error::<T, I>::SpendExpired);
			ensure!(
				matches!(spend.status, PaymentState::Pending | PaymentState::Failed),
				Error::<T, I>::AlreadyAttempted
			);

			let id = T::Paymaster::pay(&spend.beneficiary, spend.asset_kind.clone(), spend.amount)
				.map_err(|_| Error::<T, I>::PayoutError)?;

			spend.status = PaymentState::Attempted { id };
			spend.expire_at = now.saturating_add(T::PayoutPeriod::get());
			Spends::<T, I>::insert(index, spend);

			Self::deposit_event(Event::<T, I>::Paid { index, payment_id: id });

			Ok(())
		}
```
