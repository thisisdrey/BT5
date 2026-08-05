### Title
`add_tip` on BridgeHub lets a caller misattribute lost-tip refund ownership via an unauthenticated `sender` parameter - ([File: bridges/snowbridge/pallets/system-v2/src/lib.rs])

### Summary
The core broken invariant in the referenced report is that a monetary/ownership-binding value (withdrawal credentials) is keyed by a caller-supplied identifier (`pubkey`) rather than being cryptographically tied to who actually pays, letting an unrelated party's earlier write silently become authoritative for later funds. The same pattern exists in `Pallet::add_tip` in `bridges/snowbridge/pallets/system-v2/src/lib.rs`.

### Finding Description
`add_tip` is dispatched under `T::FrontendOrigin::ensure_origin(origin)`, which only authenticates that the call comes from a legitimate XCM location (the AssetHub `system-frontend` sovereign/agent forwarding a `Transact`), not that the `sender: AccountIdOf<T>` argument corresponds to the account that actually funded the tip: [1](#0-0) 

`sender` is a free-form data field, entirely decoupled from the authenticated origin. When the underlying tip application fails (e.g. `InboundQueue::add_tip`/`OutboundQueue::add_tip` returns `Err` because the nonce was already consumed), the pallet records the lost amount against whatever `sender` value was supplied:

```
if let Err(ref e) = result {
    LostTips::<T>::mutate(&sender, |lost_tip| {
        *lost_tip = lost_tip.saturating_add(amount);
    });
}
```

`LostTips` is explicitly documented as the bookkeeping used for a future refund/recovery mechanism: [2](#0-1) 

Because `sender` is never checked against the origin or against who actually transferred the tip funds on the AssetHub side, the value that determines who is entitled to recover a lost tip is bound only to a caller-supplied field — exactly the same class of defect as the deposit contract binding withdrawal credentials to an unauthenticated `pubkey` field rather than to who actually deposited the 32 ETH. Any real payer whose tip transaction fails (nonce already consumed is a normal race condition in a bridge with concurrent relayers/tippers, not a validator/relayer misbehavior) has their loss recorded under an account chosen by the forwarding logic instead of themselves, and that record permanently locks/misattributes the refundable value with no compensating check in this pallet.

### Impact Explanation
This falls within the "permanent user-fund lock" and "duplicate settlement/payout" impact classes: once a tip transfer fails, the only trace of the loss (`LostTips`) can be attributed to the wrong account, permanently preventing the actual payer from recovering funds through any future refund mechanism keyed on this storage, while potentially crediting an unrelated account. It does not require a malicious validator, relayer, or governance actor — only that the tip-adding path (nonce already consumed) fails, which happens naturally whenever a tip is submitted for a nonce that gets processed before the tip call lands (a legitimate, foreseeable race in a public bridging flow), combined with `sender` not being authenticated.

### Likelihood Explanation
Likelihood is moderate: `AddTipError::NonceConsumed` is a normal, frequently-occurring failure mode for this feature (any tip submitted concurrently with message processing), and nothing in `add_tip` validates that `sender` corresponds to whoever's funds were actually taken upstream on AssetHub. The bug triggers on ordinary usage, not on adversarial timing tricks by validators.

### Recommendation
Derive `sender` from the authenticated origin (or a value cryptographically bound to the AssetHub caller that funded the tip, verified end-to-end through the XCM `Transact`), rather than accepting it as an unchecked parameter. Alternatively, require that `LostTips` accounting be updated only via a call path where the actual token debit and the `sender` attribution happen atomically in the same authenticated context.

### Proof of Concept
1. AssetHub's `system-frontend` pallet forwards a `Transact` to `add_tip(origin=FrontendOrigin, sender=S, message_id=Inbound(nonce), amount=A)` after reserving/transferring `A` from the real payer.
2. Because `sender` is merely encoded call data (not derived from the authenticated origin), if the forwarding path or any intermediate parachain logic supplies a `sender` different from the actual payer (or if the relevant message nonce races to being consumed before this call lands), the resulting `Err(NonceConsumed)` causes `LostTips::<T>::mutate(&sender, ...)` to record the loss against `sender`, not the real payer.
3. The real payer has no route to recover the lost tip since the only bookkeeping (`LostTips`) is keyed by an unauthenticated field, and the pallet performs no verification that `sender == origin`'s effective payer prior to insertion. [1](#0-0)

### Citations

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L136-142)
```rust
	/// Relayer reward tips that were paid by the user to incentivize the processing of their
	/// message, but then could not be added to their message reward (e.g. the nonce was already
	/// processed or their order could not be found). Capturing the lost tips here supports
	/// implementing a recovery method in the future.
	#[pallet::storage]
	pub type LostTips<T: Config> =
		StorageMap<_, Blake2_128Concat, AccountIdOf<T>, u128, ValueQuery>;
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L251-281)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(<T as pallet::Config>::WeightInfo::add_tip())]
		pub fn add_tip(
			origin: OriginFor<T>,
			sender: AccountIdOf<T>,
			message_id: MessageId,
			amount: u128,
		) -> DispatchResult {
			T::FrontendOrigin::ensure_origin(origin)?;

			let result = match message_id {
				Inbound(nonce) => <T as pallet::Config>::InboundQueue::add_tip(nonce, amount),
				Outbound(nonce) => <T as pallet::Config>::OutboundQueue::add_tip(nonce, amount),
			};

			if let Err(ref e) = result {
				tracing::debug!(target: LOG_TARGET, ?e, ?message_id, ?amount, "error adding tip");
				LostTips::<T>::mutate(&sender, |lost_tip| {
					*lost_tip = lost_tip.saturating_add(amount);
				});
			}

			Self::deposit_event(Event::<T>::TipProcessed {
				sender,
				message_id,
				amount,
				success: result.is_ok(),
			});

			Ok(())
		}
```
