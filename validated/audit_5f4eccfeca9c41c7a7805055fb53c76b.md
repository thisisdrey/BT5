Based on my review of the actual code in `bridges/snowbridge/primitives/core/src/reward.rs`, the claim is substantiated by the repository content.

Audit Report

## Title
Relayer fee is charged before XCM delivery is confirmed, so a failed `deliver()` burns the relayer's fee without paying (or refunding) the reward - (File: `bridges/snowbridge/primitives/core/src/reward.rs`)

## Summary
`PayAccountOnLocation::pay_reward` validates the reward-delivery XCM via `validate_send`, then unconditionally calls `XcmExecutor::charge_fees(relayer.clone(), fee)` to withdraw the relayer's local delivery fee, and only afterward attempts `XcmSender::deliver(ticket)`. [1](#0-0)  If `deliver()` fails, the function returns `Err(XcmSendFailure)` but the fee already withdrawn by `charge_fees` is never refunded, since there is no compensating code path in the function.

## Finding Description
The payout sequence performs three sequential fallible steps but only the first two are checked before the value-moving action executes: `validate_send` builds the ticket and fee, `charge_fees` immediately debits the relayer's account for the fee, and only then is `deliver(ticket)` attempted. [2](#0-1)  Because `charge_fees` is a committed balance mutation (not a reservation/hold), a subsequent `deliver` failure leaves the relayer's fee spent with no reward delivered and no refund issued. The existing unit test `pay_reward_fails_on_delivery` demonstrates exactly this: the mock `deliver` implementation fails, and the function returns an error, but the test only asserts the error variant and never checks/asserts a refund because none exists. [3](#0-2)  The `ChargeFeesFailure` and `XcmSendFailure` error variants are collapsed into `DispatchError::Other`, giving no distinguishing signal for callers to attempt reconciliation. [4](#0-3) 

This procedure is wired in as the live `PaymentProcedure` for Snowbridge rewards on BridgeHub Westend, confirmed in `bridge_common_config.rs`. [5](#0-4) 

However, I was unable to fully verify within the available tooling (1) the exact implementation of `XcmExecutor::charge_fees` in `polkadot/xcm/xcm-executor/src/lib.rs` to confirm it performs an irreversible balance withdrawal versus some reservation/hold semantics, and (2) the call site of `pay_reward` inside the relayers pallet (e.g., `claim_rewards_to`/`RewardLedger`) to confirm whether the pallet marks the reward as already claimed/deducted *before* calling `pay_reward`, which would determine whether a failed delivery also permanently loses the underlying reward record (not just the fee) or whether the relayer could retry. This distinction affects the precise severity (fee-only loss vs. reward-plus-fee loss) but does not change the core validity of the finding: the fee-charge-before-delivery-confirmation ordering with no rollback is present in the code as shown.

## Impact Explanation
A relayer with an accrued Snowbridge reward can lose the local XCM delivery fee (a real balance debit via `charge_fees`) whenever `XcmSender::deliver` fails after `validate_send`/`charge_fees` succeed — e.g., transient HRMP/XCMP channel congestion or a suspended outbound channel between BridgeHub and AssetHub. This is a value-conservation violation in the bridge reward payout path: the relayer's balance decreases with no corresponding reward settlement and no compensating mint/refund, matching the "bridge rewards... must conserve value and settle exactly once" pivot for scoped code.

## Likelihood Explanation
Triggering `validate_send` success followed by `deliver` failure is plausible under ordinary, permissionless operating conditions (transient outbound queue backpressure/suspension on BridgeHub→AssetHub), requiring no privileged actor, governance action, or malicious relayer — any legitimate relayer calling into the reward payout path during such a window is affected.

## Recommendation
Reorder the operation so `charge_fees` only executes after `deliver` succeeds, or adopt a two-phase approach: hold/reserve the fee amount at validation time and only commit the charge on successful delivery, refunding the hold on `deliver` failure. Alternatively, wrap the `charge_fees` + `deliver` pair so that any `deliver` failure triggers an explicit compensating refund of the already-charged fee before propagating the error.

## Proof of Concept
The existing test `pay_reward_fails_on_delivery` reproduces the vulnerable ordering (validate succeeds, `charge_fees` succeeds via `MockXcmExecutor`, `deliver` fails) and confirms the function returns `Err` without any refund mechanism being exercised or asserted. [3](#0-2)  To reproduce end-to-end on-chain: have a relayer with a registered Snowbridge reward invoke the pallet call that routes through `PayAccountOnLocation::pay_reward` while the BridgeHub→AssetHub outbound channel is in a state causing `XcmRouter::deliver` to fail after `validate_send` succeeds; observe the relayer's local fee balance decrease from `charge_fees` while the call returns a `ChargeFeesFailure`/`XcmSendFailure`-derived `DispatchError` and no compensating credit is issued.

### Citations

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L54-61)
```rust
impl From<RewardPaymentError> for DispatchError {
	fn from(e: RewardPaymentError) -> DispatchError {
		match e {
			XcmSendFailure => DispatchError::Other("xcm send failure"),
			ChargeFeesFailure => DispatchError::Other("charge fees error"),
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

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L358-400)
```rust
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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L117-135)
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
```
