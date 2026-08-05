All code citations in the report check out exactly against the repository. `do_claim_rewards` at bridges/modules/relayers/src/lib.rs takes and removes the `RelayerRewards` entry, calls `T::PaymentProcedure::pay_reward`, and only on `Ok` emits `RewardPaid` — all within a single atomic `try_mutate_exists` closure, with no mechanism to restore the entry if the reward is later found not to have landed on the destination. [1](#0-0)  `PayAccountOnLocation::pay_reward` (the `PaymentProcedure` wired in for `BridgeReward::Snowbridge`) builds an unconfirmed `UnpaidExecution`/`ReserveAssetDeposited`/`DepositAsset` program and returns `Ok(())` as soon as `XcmSender::deliver` succeeds — i.e., once the message is handed to the transport layer, not once `DepositAsset` actually executes on AssetHub. [2](#0-1)  There is no `SetAppendix`/`ReportError`/query-response wiring attached to this XCM program, so BridgeHub has no way to learn about remote execution failure (e.g., unregistered foreign asset, unresolvable beneficiary, ED failure), and the pallet's own test suite only covers the "delivery succeeds"/"delivery fails" cases, not "delivery succeeds but remote execution fails." [3](#0-2) 

This matches the Polkadot SDK Impact Gate's "permanent user-fund ... lock" / non-atomic settlement category: reward-payout state (`RelayerRewards`) advances to "paid" based purely on successful XCM hand-off rather than confirmed execution and settlement at the beneficiary, which is exactly the kind of non-atomic cross-chain settlement issue the pivots call out ("Message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically"). The exploit path requires no privileged actor — any relayer with an accrued Snowbridge reward calling `claim_rewards_to` under an ordinary remote-execution failure condition on AssetHub triggers irreversible loss, matching the required "unprivileged external attacker using public extrinsics" criterion (the "attacker" here need not even be malicious — normal usage under a realistic failure condition suffices).

Audit Report

## Title
Reward claims are marked paid and cleared from storage before the cross-chain deposit is confirmed, causing silent, unrecoverable fund loss for Snowbridge relayers - (File: `bridges/snowbridge/primitives/core/src/reward.rs`)

## Summary
`PayAccountOnLocation::pay_reward`, used as the `PaymentProcedure` for `BridgeReward::Snowbridge`, sends a one-way, unconfirmed XCM (`UnpaidExecution` + `ReserveAssetDeposited` + `DepositAsset`) to AssetHub and returns `Ok(())` the moment the message is handed to `XcmSender::deliver`, well before the remote chain executes `DepositAsset`. `do_claim_rewards` treats this `Ok(())` as final settlement: it removes the relayer's `RelayerRewards` entry and emits `RewardPaid` atomically, so if the remote `DepositAsset` traps or fails, the relayer's reward is permanently and unrecoverably lost.

## Finding Description
`do_claim_rewards` takes and clears the `RelayerRewards` entry, then calls `T::PaymentProcedure::pay_reward`, and only emits `RewardPaid` on `Ok` — all inside one atomic `try_mutate_exists` closure:
```rust
RelayerRewards::<T, I>::try_mutate_exists(&relayer, reward_kind, |maybe_reward| -> DispatchResult {
    let reward_balance = maybe_reward.take().ok_or(Error::<T, I>::NoRewardForRelayer)?;
    T::PaymentProcedure::pay_reward(&relayer, reward_kind, reward_balance, beneficiary.clone())
        .map_err(|e| { ... Error::<T, I>::FailedToPayReward })?;
    Self::deposit_event(Event::<T, I>::RewardPaid { .. });
    Ok(())
})
``` [1](#0-0) 

For `PayAccountOnLocation` (used for `BridgeReward::Snowbridge`), `pay_reward` only validates, charges local delivery fees, and dispatches the XCM — it never confirms remote execution:
```rust
let (ticket, fee) = validate_send::<XcmSender>(AssetHubLocation::get(), xcm).map_err(|_| XcmSendFailure)?;
XcmExecutor::charge_fees(relayer.clone(), fee).map_err(|_| ChargeFeesFailure)?;
XcmSender::deliver(ticket).map_err(|_| XcmSendFailure)?;
Ok(())
``` [4](#0-3) 

The XCM program built has no `SetAppendix`/`ReportError`/query-response callback:
```rust
let xcm: Xcm<()> = alloc::vec![
    UnpaidExecution { weight_limit: Unlimited, check_origin: None },
    DescendOrigin(InboundQueueLocation::get().into()),
    UniversalOrigin(GlobalConsensus(EthereumNetwork::get())),
    ReserveAssetDeposited(assets.into()),
    DepositAsset { assets: AllCounted(1).into(), beneficiary },
]
``` [5](#0-4) 

So BridgeHub never learns whether `DepositAsset` succeeded on AssetHub. If the remote execution fails (unregistered foreign asset, unresolvable beneficiary sub-location, existential-deposit failure, barrier/filter rejection), funds are trapped/dropped on AssetHub with no error propagated back — yet `RelayerRewards` has already been irreversibly cleared and `RewardPaid` already emitted on BridgeHub. The existing unit tests only cover "delivery succeeds" (`pay_reward_success`) and "delivery itself fails" (`pay_reward_fails_on_xcm_validate_xcm`, `pay_reward_fails_on_charge_fees`, `pay_reward_fails_on_delivery`) — none exercise "delivery succeeds but remote execution fails," which is the actual vulnerable path. [6](#0-5) 

## Impact Explanation
A relayer's Snowbridge reward claim can be permanently and silently voided: `RelayerRewards` is cleared so any retry returns `Error::<T, I>::NoRewardForRelayer`, yet no tokens are deposited on AssetHub if remote execution fails. This is a permanent, unrecoverable loss of relayer funds and a violation of one-time, atomic settlement of bridge-reward payout state, matching the "permanent user-fund ... lock" / non-atomic settlement impact category.

## Likelihood Explanation
No privileged actor, malicious relayer, validator, or governance action is required — only an ordinary condition where AssetHub-side execution of the forwarded XCM fails (e.g., the reward's foreign asset not yet registered on AssetHub, or a beneficiary location that cannot resolve to a valid account). Any relayer accruing a reward via the standard `register_reward` flow and calling the public `claim_rewards`/`claim_rewards_to` extrinsics can trigger this under such conditions, and the failure is not covered or defended against by any existing code path or test.

## Recommendation
Do not treat `XcmSender::deliver` success as final settlement for `PayAccountOnLocation`. Use a confirmed/query-based payment mechanism (e.g., XCM `QueryResponse`/`ReportError` callbacks, similar to `PayOverXcm` in `polkadot/xcm/xcm-builder/src/pay.rs`) and only clear `RelayerRewards` once success is confirmed, or move the reward to a re-claimable "pending confirmation" state instead of deleting it outright, with a reconciliation/refund mechanism for remote execution failures.

## Proof of Concept
1. A relayer accrues a `BridgeReward::Snowbridge` reward via `register_reward`, confirmed by `RewardRegistered`.
2. Relayer calls `BridgeRelayers::claim_rewards_to(origin, BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(loc))`.
3. Inside `do_claim_rewards`, the `RelayerRewards` entry is taken (removed), and `PayAccountOnLocation::pay_reward` builds the `ReserveAssetDeposited`/`DepositAsset` XCM and calls `XcmSender::deliver`.
4. As long as delivery to AssetHub succeeds, `pay_reward` returns `Ok(())`; `RewardPaid` is emitted and the reward entry is gone from `RelayerRewards`, regardless of what happens when the message executes on AssetHub.
5. If, on AssetHub, `DepositAsset` traps (e.g., the reward's asset isn't registered, or the beneficiary sub-location cannot be resolved into a valid account), the relayer receives nothing and cannot re-claim: `claim_rewards`/`claim_rewards_to` for that `reward_kind` now returns `Error::<T, I>::NoRewardForRelayer`.

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

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L246-400)
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
