### Title
Snowbridge relayer reward is irrecoverably lost when the XCM-based payout to AssetHub fails after `BridgeRelayers::claim_rewards_to` has already deleted the reward entry - ([File: bridges/snowbridge/primitives/core/src/reward.rs])

### Summary
### Finding Description
`pallet-bridge-relayers::claim_rewards_to` calls `do_claim_rewards`, which uses `RelayerRewards::try_mutate_exists` to remove the relayer's pending reward from storage and only re-inserts it if the `PaymentProcedure::pay_reward` closure returns `Err`: [1](#0-0) 

For Snowbridge rewards, `PaymentProcedure::pay_reward` is implemented by `BridgeRewardPayer::pay_reward`, which for a `BridgeRewardBeneficiaries::AssetHubLocation` beneficiary delegates to `snowbridge_core::reward::PayAccountOnLocation::pay_reward`: [2](#0-1) 

`PayAccountOnLocation::pay_reward` builds an XCM containing `UnpaidExecution`, `DescendOrigin(InboundQueueLocation)`, `UniversalOrigin(GlobalConsensus(EthereumNetwork))`, `ReserveAssetDeposited`, and `DepositAsset { beneficiary, .. }`, and sends it to AssetHub. Crucially, the function only returns `Err` if `validate_send`, `charge_fees`, or `deliver` fail on BridgeHub itself; once the ticket is handed to the XCM transport layer, it returns `Ok(())` regardless of whether the `DepositAsset` instruction actually succeeds when the message is *executed* on AssetHub: [3](#0-2) 

Because `pay_reward` returns `Ok(())` based solely on successful *delivery* (an asynchronous cross-chain send), `do_claim_rewards` on BridgeHub permanently removes the `RelayerRewards` entry and emits `Event::RewardPaid` before the corresponding `DepositAsset` on AssetHub has actually settled. If `DepositAsset` fails on AssetHub — e.g. the derived/foreign asset for the Ethereum reserve isn't registered for that beneficiary, the beneficiary account cannot receive the asset (below existential deposit, wrong asset filters, etc.) — the assets from `ReserveAssetDeposited` are trapped in AssetHub's asset-trap register instead of being deposited. The trap is recorded under the XCM origin in effect at that point in the program, which was overridden by `UniversalOrigin(GlobalConsensus(EthereumNetwork))` combined with `DescendOrigin(InboundQueueLocation)` — a location that no local signed account on AssetHub can reproduce via the ordinary `OriginConverter`/`SignedToAccountId32` used by `pallet_xcm::claim_assets`. The relayer therefore has no practical way to reclaim the trapped reward.

This mirrors the `ETHCrowdfundBase` bug class in a stronger sense than a simple "reverting recipient": here the accounting layer (BridgeHub) treats the payout as final and settled ("Paid") purely on send success, while the actual value-transfer step (AssetHub `DepositAsset`) is decoupled, asynchronous, and can fail independently — violating the required invariant that "payout state must only advance after decode, dispatch, execution, and settlement succeed atomically."

### Impact Explanation
A relayer's earned reward can be permanently and unrecoverably lost even though the pallet's bookkeeping (`RelayerRewards` storage, `RewardPaid` event) reports it as successfully paid. This is a genuine fund-loss / permanent-lock bug in the reward-payout settlement path of the Snowbridge delivery flow, matching the "permanent user-fund or bridge-state lock" and "payout state must only advance after ... settlement succeed atomically" impact categories.

### Likelihood Explanation
No privileged actor, governance action, or malicious peer/relayer/validator is required. `claim_rewards_to` is a public, unprivileged, signed extrinsic and the caller (the relayer claiming their own reward) supplies the `beneficiary` `VersionedLocation` themselves: [4](#0-3) 
A relayer specifying (by mistake, or a beneficiary whose registration/ED state changes between construction and execution) a beneficiary Location that cannot accept the reserve-deposited asset on AssetHub triggers the loss deterministically and reproducibly — the on-chain test suite even already demonstrates `claim_rewards_to` failing/being rejected under certain HRMP/setup conditions, showing the fragility of this "fire-and-forget" settlement design: [5](#0-4) 

### Recommendation
Do not treat the reward as settled/removed from `RelayerRewards` based on XCM *send* success alone. Options:
- Keep the reward pending until a delivery/execution confirmation (e.g. via a report-back XCM `QueryResponse` or a receipt acknowledging successful `DepositAsset`) is received from AssetHub, only then clearing `RelayerRewards`.
- Alternatively, use a `Transact`+response pattern or `SetAppendix`/`SetErrorHandler` to route trapped/failed deposits back to a beneficiary the relayer can actually control and claim from (e.g., their own local BridgeHub-controlled origin), instead of an unreachable `UniversalOrigin` context.
- As a minimal mitigation matching the referenced report's guidance, keep a re-claimable pending state (similar to `pallet-treasury`'s `PaymentState::Attempted`/`check_status`/retry flow) instead of unconditionally finalizing the reward on `Ok(())` from `pay_reward`.

### Proof of Concept
1. Relayer accrues a Snowbridge reward via `register_reward`, visible in `RelayerRewards`.
2. Relayer calls `BridgeRelayers::claim_rewards_to(origin, BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(loc))` where `loc` is a Location that will fail `DepositAsset` on AssetHub (e.g., an account without existential deposit for the derived Ethereum-reserve asset, or where the asset isn't registered for that beneficiary filter).
3. On BridgeHub: `PayAccountOnLocation::pay_reward` succeeds (`validate_send`, `charge_fees`, `deliver` all succeed) → returns `Ok(())`.
4. `do_claim_rewards`'s `try_mutate_exists` commits: `RelayerRewards` entry is removed, `Event::RewardPaid` is emitted.
5. On AssetHub: the XCM executes; `ReserveAssetDeposited` places the Ethereum-reserve asset into holding, but `DepositAsset { beneficiary, .. }` fails against the ill-formed/incapable beneficiary; the asset is trapped under the `UniversalOrigin(Ethereum)+DescendOrigin(InboundQueueLocation)` origin.
6. Relayer has no signed origin on AssetHub matching that trap origin, so `pallet_xcm::claim_assets` cannot recover the funds — the reward is permanently lost despite BridgeHub state showing it as paid. [6](#0-5)

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

**File:** bridges/modules/relayers/src/lib.rs (L263-300)
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
```

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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/tests/tests.rs (L777-795)
```rust
			let claim_location = VersionedLocation::V5(Location::new(
				1,
				[
					Parachain(1000),
					xcm::latest::Junction::AccountId32 {
						id: account2.clone().into(),
						network: None,
					},
				],
			));
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
