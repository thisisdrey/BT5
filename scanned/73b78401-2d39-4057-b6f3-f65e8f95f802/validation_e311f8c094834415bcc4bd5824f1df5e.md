### Title
Reward settlement finalized on XCM send success, not remote mint success — permanent relayer-reward loss - (File: `bridges/snowbridge/primitives/core/src/reward.rs`)

### Summary
`PayAccountOnLocation::pay_reward` (the `PaymentProcedure` used for `BridgeReward::Snowbridge`) treats a relayer reward as "paid" as soon as the reward-minting XCM to AssetHub has been *sent*, without any confirmation that the `ReserveAssetDeposited`/`DepositAsset` instructions actually execute successfully on the remote chain. This mirrors the Synthetix bug pattern: an operation that depends on a remote/external component (here, XCM execution and asset registration on AssetHub) is assumed to always succeed, and the caller's local bookkeeping is finalized before that assumption is verified.

### Finding Description
`pay_reward` builds an `UnpaidExecution` XCM containing `DescendOrigin`, `UniversalOrigin`, `ReserveAssetDeposited`, `DepositAsset`, sends it to AssetHub, and returns `Ok(())` as soon as `validate_send`, `charge_fees`, and `deliver` succeed: [1](#0-0) 

None of these three checks say anything about what happens once the message actually lands and executes on AssetHub. If the remote `DepositAsset` fails — e.g. the beneficiary `Location` doesn't convert to a valid account, the `ether` foreign asset isn't registered, the account is below existential deposit, or a filter/barrier on AssetHub rejects `UnpaidExecution` from that derived origin — the assets are trapped or dropped on AssetHub, but `pay_reward` has already returned `Ok(())`.

The caller, `do_claim_rewards`, takes this `Ok(())` at face value and permanently clears the relayer's stored reward and emits `RewardPaid`: [2](#0-1) 

Because `RelayerRewards::try_mutate_exists` only rolls back the `RelayerRewards` entry if the *closure* returns `Err`, and `pay_reward` returns `Ok` whenever the send succeeds (irrespective of remote settlement), the relayer's earned reward entry is deleted and cannot be reclaimed even though the value was never actually delivered to the beneficiary. The `claim_rewards_to` extrinsic requires only `ensure_signed`, and the `beneficiary: Location` is fully attacker/user-controlled (`BridgeRewardBeneficiaries::AssetHubLocation(VersionedLocation)`), so any malformed, unsupported, or later-invalidated destination causes irreversible loss with no receipt/confirmation mechanism to retry or refund: [3](#0-2) 

The existing test suite only exercises the "send succeeded" cases (`pay_reward_success`, `pay_reward_fails_on_xcm_validate_xcm`, `pay_reward_fails_on_charge_fees`, `pay_reward_fails_on_delivery`); there is no test or code path validating that the remote `DepositAsset` actually completed: [4](#0-3) 

This is the exact same failure mode as the Synthetix report: an assumed-successful external/remote interaction (`wrapper.mint()` on OP chain vs. `DepositAsset` execution on AssetHub) is not actually verified, so the calling logic (`liquidationType2` / `do_claim_rewards`) commits to an outcome ("liquidation done" / "reward paid") that never materializes.

### Impact Explanation
This breaks the pivot requirement that "payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." A relayer's legitimately earned `Snowbridge` reward can be permanently and irrecoverably lost — the reward entry is deleted from storage, `RewardPaid` is emitted, delivery fees are charged, yet no value reaches any beneficiary if the remote AssetHub execution fails. This is a fund-loss/fund-lock bug in the bridge reward-payout path with no admin, governance, or malicious-actor precondition.

### Likelihood Explanation
The `beneficiary` location is supplied directly by the calling relayer via `claim_rewards_to`, and cross-chain XCM execution failure conditions (unregistered asset changes, barrier updates, mis-encoded locations, insufficient ED on the derived account) are realistic and not under the relayer's control once the message is in flight. Since `pay_reward`'s success criterion is purely local (message accepted for delivery), this can be triggered unintentionally by ordinary relayer usage, not just by a sophisticated attacker.

### Recommendation
Do not finalize (remove) the `RelayerRewards` entry and emit `RewardPaid` until remote settlement is confirmed. Options:
- Use a receipt/acknowledgment mechanism (similar to `process_delivery_receipt` in the outbound queue) so AssetHub reports back successful `DepositAsset` execution before BridgeHub clears the reward.
- Alternatively, keep the funds in an escrow/claimable state on BridgeHub and only transfer/burn upon confirmed remote success, allowing retry with a corrected beneficiary if the first attempt is trapped.
- At minimum, validate the beneficiary `Location` against a known-good, chain-verified conversion/registration before consuming the local reward.

### Proof of Concept
1. Relayer accrues a `Snowbridge` reward via `register_reward`.
2. Relayer calls `claim_rewards_to` with `BridgeRewardBeneficiaries::AssetHubLocation(location)` where `location` fails to convert to a valid AssetHub account, or where the `ether` foreign asset deposit trips a filter/registration issue on AssetHub (any condition causing `DepositAsset` to trap remotely).
3. On BridgeHub: `validate_send`, `charge_fees`, `deliver` all succeed → `pay_reward` returns `Ok(())` → `RelayerRewards` entry is cleared and `RewardPaid` is emitted.
4. On AssetHub: the XCM executes but `DepositAsset` fails/traps; no `pallet_assets::Event::Deposited` for the intended beneficiary is produced (contrast with the success path shown in `claim_rewards_works`, where the deposit event on AssetHub is explicitly checked at [5](#0-4) ).
5. Relayer has no reward entry left to reclaim (`NoRewardForRelayer` on retry) and no funds were delivered — reward permanently lost.

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
