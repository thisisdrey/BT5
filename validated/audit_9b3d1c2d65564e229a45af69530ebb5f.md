### Title
Ignite-style unrefunded fee: AssetHub charges XCM delivery+execution fee for `set_keys`/`purge_keys` before the Relay Chain confirms the operation, with no refund path if the RC-side registration silently fails - ([File: substrate/frame/staking-async/rc-client/src/lib.rs])

### Summary
`pallet-staking-async-rc-client::set_keys` (AssetHub) charges the full XCM fee (delivery + remote execution cost) from the stash's liquid balance *before* the Relay Chain actually executes the registration (`SessionInterface::set_keys`). If the RC-side call fails, `pallet-staking-async-ah-client::set_keys_from_ah` swallows the error, emits `SessionKeysUpdateFailed`, and returns `Ok(())` — meaning the AH extrinsic that charged the fee also succeeds, but the keys were never actually set. There is no mechanism to refund the AH-side fee when this happens, mirroring the BENQI Ignite bug where a pre-charged registration fee is not returned when the registration ultimately fails.

### Finding Description
In `set_keys` [1](#0-0) , AssetHub validates the keys/proof locally, then calls `T::SendToRelayChain::set_keys`, which is implemented via `XCMSender::send_with_fees` [2](#0-1) . This function calls `XcmExec::charge_fees(payer_location, total_assets)` (irreversibly withdrawing the total fee from the stash) and only *afterwards* calls `Sender::deliver(ticket)` to actually send the Transact message to the Relay Chain.

On the Relay Chain, `pallet-staking-async-ah-client::set_keys_from_ah` receives the message and calls `T::SessionInterface::set_keys(&stash, session_keys)`. If that call returns an `Err`, the pallet does **not** propagate the error back — it logs a warning, emits `Event::SessionKeysUpdateFailed { stash, update, error }`, and returns `Ok(())` [3](#0-2) . This is confirmed directly by the test `set_keys_from_ah`, which shows that when `SessionInterface::set_keys` fails, the call still returns `Ok`, no keys are set, and only an event is emitted [4](#0-3) .

Because the RC-side failure is asynchronous (delivered via a one-way XCM `Transact`) and does not report back to AssetHub, and because the AH-side fee was already withdrawn from the stash's balance prior to delivery, there is no code path on AssetHub that detects the RC-side failure and refunds the `total_fee` (delivery + execution cost) that was charged. The `FeesPaid` event on AH is emitted unconditionally on successful *send*, not on successful *registration* [5](#0-4) .

This is the direct analog of the Ignite bug: a fee is deducted at the "submission" step for an operation whose actual success/failure is determined by a later, separate stage; when that later stage fails, the previously-collected fee is not returned, unlike other failure paths in the same pallet (e.g., `purge_keys`'s deposit-hold release is rolled back transactionally when the *local* XCM send itself fails — see `deposit_lifecycle` test — but that only protects against local-send failure, not against a downstream RC-side rejection of the actual registration).

### Impact Explanation
A validator who calls `set_keys` pays a non-trivial XCM fee (delivery fee + `RemoteKeysExecutionWeight`-derived execution cost, deliberately overcharged 2-3x per the code comment) every time. If `SessionInterface::set_keys` on the Relay Chain rejects the operation (e.g., due to a state mismatch, a duplicate-key conflict enforced by `pallet_session` that AssetHub cannot pre-validate since AH only checks proof validity, not RC-wide key uniqueness, or any other RC-side business-logic failure), the validator's keys are silently not updated but the fee is permanently lost. This is a value-conservation violation: funds are taken for a service that was not rendered, with no automatic or user-triggerable refund, and the failure is only observable via an on-chain event (`SessionKeysUpdateFailed`) that ordinary users are unlikely to monitor.

### Likelihood Explanation
This does not require a malicious actor, governance, or a compromised relayer — it is a normal user (validator/stash) calling a public, unprivileged extrinsic (`set_keys`, and `purge_keys` has the analogous fee-charge-before-RC-confirmation pattern). The trigger condition is any RC-side rejection of `SessionInterface::set_keys` after AH-side validation succeeds — plausible whenever AH's local checks (proof validity, decode format) diverge from RC's actual state (e.g., key already claimed by another stash, `pallet_session`-level `DuplicatedKey`-style checks, or any future validation added on the RC side that AH does not mirror). Given AH explicitly documents that "RC trusts AH's validation and does not re-validate," any drift between the two validation surfaces reproduces this loss deterministically.

### Recommendation
Do not treat fee-charging as final until RC-side registration success is confirmed. Options:
- Have `ah-client::set_keys_from_ah` return the error via a dispatchable result or a reporting mechanism back to `rc-client` (e.g., a confirmation message analogous to `validator_set`) so AssetHub can refund the previously-charged fee when `SessionKeysUpdateFailed` occurs.
- Alternatively, replicate the RC-side key-uniqueness/validation logic on AssetHub before charging fees, eliminating the class of RC-side-only failures.
- At minimum, make the failure path auditable/refundable by storing the charged fee against the stash and exposing a `claim_failed_set_keys_fee` extrinsic triggered from the `SessionKeysUpdateFailed` condition once relayed back.

### Proof of Concept
1. On AssetHub, validator `stash` calls `rc_client::Pallet::set_keys(origin, keys, proof, None)` with keys/proof that pass AH's local `RelayChainSessionKeys::decode` and `ownership_proof_is_valid` checks.
2. `T::SendToRelayChain::set_keys` charges `total_fee = delivery_fee + execution_cost` from `stash` via `XcmExec::charge_fees`, then delivers the XCM Transact — see `send_with_fees` [2](#0-1) . `set_keys` on AH returns `Ok(())` and emits `FeesPaid`.
3. On the Relay Chain, `ah_client::set_keys_from_ah` is dispatched with `stash`/`keys`, but `T::SessionInterface::set_keys(&stash, session_keys)` returns `Err(error)` (e.g. simulate with the test harness's `SetKeysError::set(Some(error))`, as shown in `keys_from_ah_tests::set_keys_from_ah` [4](#0-3) ).
4. Result: `pallet_session::NextKeys` for `stash` is never updated on RC, an `Event::SessionKeysUpdateFailed` is emitted, and `set_keys_from_ah` still returns `Ok(())`.
5. On AssetHub, `stash`'s balance is permanently reduced by `total_fee`, with no corresponding successful registration and no refund code path anywhere in `rc-client` or `ah-client`.

### Citations

**File:** substrate/frame/staking-async/rc-client/src/lib.rs (L716-733)
```rust
		// Charge the total fee from the payer using the same asset as delivery fees
		let total_assets = xcm::latest::Assets::from(xcm::latest::Asset {
			id: fee_asset.id.clone(),
			fun: Fungible(total_fee.into()),
		});

		XcmExec::charge_fees(payer_location, total_assets).map_err(|e| {
			log::error!(target: LOG_TARGET, "Failed to charge fees: {:?}", e);
			SendKeysError::Send(SendOperationError::ChargeFeesFailed)
		})?;

		Sender::deliver(ticket).map_err(|e| {
			log::error!(target: LOG_TARGET, "Failed to deliver XCM: {:?}", e);
			SendKeysError::Send(SendOperationError::DeliveryFailed)
		})?;

		Ok(total_fee)
	}
```

**File:** substrate/frame/staking-async/rc-client/src/lib.rs (L1310-1356)
```rust
		pub fn set_keys(
			origin: OriginFor<T>,
			keys: Vec<u8>,
			proof: Vec<u8>,
			max_delivery_and_remote_execution_fee: Option<BalanceOf<T>>,
		) -> DispatchResult {
			let stash = ensure_signed(origin)?;

			// Only registered validators can set session keys
			ensure!(T::AHStakingInterface::is_validator(&stash), Error::<T>::NotValidator);

			// Hold deposit for key storage.
			let deposit = T::KeyDeposit::get();
			if !deposit.is_zero() {
				let current_hold = T::Currency::balance_on_hold(&HoldReason::Keys.into(), &stash);
				if current_hold < deposit {
					// Top up if current hold is below the required deposit.
					T::Currency::set_on_hold(&HoldReason::Keys.into(), &stash, deposit)?;
				}
			}

			// Validate keys: decode as RelayChainSessionKeys to ensure correct format
			let session_keys = T::RelayChainSessionKeys::decode(&mut &keys[..])
				.map_err(|_| Error::<T>::InvalidKeys)?;

			// Validate ownership proof
			ensure!(
				session_keys.ownership_proof_is_valid(&stash.encode(), &proof),
				Error::<T>::InvalidProof
			);

			// Forward validated keys to RC (no proof needed, already validated)
			let fees = T::SendToRelayChain::set_keys(
				stash.clone(),
				keys,
				max_delivery_and_remote_execution_fee,
			)
			.map_err(|e| match e {
				SendKeysError::Send(_) => Error::<T>::XcmSendFailed,
				SendKeysError::FeesExceededMax { .. } => Error::<T>::FeesExceededMax,
			})?;
			Self::deposit_event(Event::FeesPaid { who: stash.clone(), fees });

			log::info!(target: LOG_TARGET, "Session keys validated and set for {stash:?}, forwarded to RC");

			Ok(())
		}
```

**File:** substrate/frame/staking-async/ah-client/src/lib.rs (L680-699)
```rust
			match T::SessionInterface::set_keys(&stash, session_keys) {
				Ok(()) => Self::deposit_event(Event::SessionKeysUpdated {
					stash,
					update: SessionKeysUpdate::Set,
				}),
				Err(error) => {
					log!(
						warn,
						"SessionKeysUpdateFailed: set_keys failed for {:?}: {:?}",
						stash,
						error
					);
					Self::deposit_event(Event::SessionKeysUpdateFailed {
						stash,
						update: SessionKeysUpdate::Set,
						error,
					});
				},
			}
			Ok(())
```

**File:** substrate/frame/staking-async/ah-client/src/lib.rs (L1144-1164)
```rust
			// emits SessionKeysUpdateFailed when SessionInterface::set_keys fails
			hypothetically!({
				SetKeysCalls::take();
				let error = DispatchError::Corruption;
				SetKeysError::set(Some(error));
				assert_ok!(StakingAsyncAhClient::set_keys_from_ah(
					RuntimeOrigin::root(),
					stash,
					keys.encode(),
				));
				assert!(SetKeysCalls::get().is_empty());
				System::assert_has_event(
					Event::<Test>::SessionKeysUpdateFailed {
						stash,
						update: SessionKeysUpdate::Set,
						error,
					}
					.into(),
				);
				SetKeysError::take();
			});
```
