## Analysis

The Symmetrical bug's core broken invariant is: **a value the victim fully controls and can cheaply mutate (their own account nonce) gates whether a third party's already-obtained, pre-signed authorization can execute — so the victim can permanently invalidate that pending third-party action by touching the shared nonce via any unrelated call.**

The closest local, provable analog is `pallet_meta_tx` in this repo, which lets a signer authorize a call off-chain and share it with a relayer, who dispatches it later via `MetaTx::dispatch`. The meta-tx's validity is gated by `frame_system::CheckNonce`, and it reuses the **exact same on-chain nonce storage item** (`frame_system::Account<T>::nonce`) that ordinary signed extrinsics use and increment. This is directly demonstrated by the pallet's own test: [1](#0-0) 

which explicitly comments: *"increment alice's nonce to invalidate the meta tx and verify that the meta tx extension works"* — proving the pallet authors are aware that any unrelated transaction from the signer silently invalidates an outstanding, already-shared meta-transaction, causing `Error::<T>::Stale`.

### Title
Meta-transaction authorization is invalidated by the signer's ordinary, unrelated transactions via shared account-nonce coupling - (File: `substrate/frame/meta-tx/src/lib.rs`)

### Summary
`pallet_meta_tx` allows a signer to pre-authorize a call for a relayer to submit and pay fees for. Validity of the meta-tx is bound to `frame_system::CheckNonce`, which reads and consumes the *same* `frame_system::Account<T>::nonce` field used by all of the signer's ordinary signed extrinsics. Because this nonce is a single, shared, freely-mutable counter, the signer can invalidate any outstanding meta-transaction — even one they committed to and shared with a relayer for a beneficial third party — simply by submitting (or having submitted) *any* unrelated normal transaction that increments their nonce, exactly mirroring the Symmetrical pattern where PartyA's own nonce, shared between unrelated self-service calls and the oracle-signed liquidation check, is abused to block a third party's action.

### Finding Description
- `MetaTx::dispatch` executes `meta_tx.extension.dispatch_transaction(...)`, where the extension tuple includes `frame_system::CheckNonce<Runtime>` bound to the signer's account: [2](#0-1) 
- `CheckNonce::validate_nonce_for_account` compares the meta-tx's embedded nonce against `crate::Account::<T>::get(who).nonce` — the *global* per-account nonce, not a nonce namespaced to meta-transactions: [3](#0-2) 
- Any ordinary signed extrinsic from the same account (a `balances::transfer`, a `remark`, etc.) advances this same nonce via `prepare_nonce_for_account`, with no distinction between "used for a meta-tx" and "used for a normal transaction": [4](#0-3) 
- The pallet's own regression test proves the outcome: after `frame_system::Pallet::<Runtime>::inc_account_nonce(alice_account.clone())` is called (standing in for *any* subsequent extrinsic from Alice), the previously valid, already-shared meta-tx fails with `Error::<Runtime>::Stale` when the relayer submits it: [5](#0-4) 

No guard exists to reserve or namespace a nonce range/slot for outstanding meta-transactions, and no mechanism separates "self-initiated" nonce consumption from "meta-tx-authorized" nonce consumption — precisely the missing invariant in the Symmetrical report, applied here to an unprivileged, permissionless, purely on-chain mechanism (no relayer/prover/admin trust assumptions required).

### Impact Explanation
A signer who wants to later renege on a shared, already-signed authorization (e.g., a payout, an approval, or any beneficial action promised to a counterparty and relayed on their behalf) can unilaterally and cheaply void it post hoc by submitting any unrelated transaction, without needing to build a special "cancel" call or interact with the meta-tx pallet at all. This breaks the trust model relayers and counterparties rely on when accepting a signed meta-tx as a commitment: the relayer wastes the transaction fee for the failed `dispatch` call (paid regardless, since the pallet call only errors internally rather than failing extrinsic validation), and the intended beneficiary never receives the committed execution — a direct parallel to Symmetrical's PartyA blocking their own liquidation to protect themselves at the counterparty's expense.

### Likelihood Explanation
High. No privileged access, malicious peer, or governance action is needed — the signer, an ordinary unprivileged account, achieves this purely by conducting normal chain activity (their nonce advances on every signed extrinsic they submit for any reason), making accidental or deliberate invalidation trivially likely in any deployment that exposes `pallet_meta_tx` (e.g., collectives-westend, kitchensink) whenever a signer has an incentive to back out of a previously shared commitment.

### Recommendation
Do not couple meta-transaction authorization to the same global `frame_system` account nonce used for ordinary extrinsics. Introduce a dedicated, monotonically-increasing meta-tx nonce namespace per account (separate storage item) that is only consumed by `pallet_meta_tx::dispatch`, so that unrelated signer activity cannot silently invalidate outstanding, shared authorizations. Alternatively, require explicit, auditable cancellation (a dedicated `revoke` call) rather than allowing incidental nonce growth to serve as implicit revocation.

### Proof of Concept
The existing pallet test already constitutes a full PoC of the invalidation path: [6](#0-5) 
1. Alice signs a meta-tx (e.g., authorizing a `remark_with_event` or, in a real deployment, a value-transfer/approval call) using her current nonce and shares it with relayer Bob.
2. Before Bob submits it, Alice submits (or the runtime processes) any unrelated transaction from her account, incrementing `frame_system::Account::<Runtime>::get(alice).nonce`.
3. Bob submits `MetaTx::dispatch` with the original meta-tx; `CheckNonce::validate_nonce_for_account` now sees a stale nonce and the call fails with `Error::<Runtime>::Stale`, exactly as asserted at line 312 of the test — the committed action never executes, while Bob still incurs the extrinsic's dispatch fee.

### Citations

**File:** substrate/frame/meta-tx/src/tests.rs (L252-317)
```rust
#[cfg(not(feature = "runtime-benchmarks"))]
#[test]
fn meta_tx_extension_work() {
	new_test_ext().execute_with(|| {
		// meta tx signer
		let alice_keyring = Sr25519Keyring::Alice;
		// meta tx relayer
		let bob_keyring = Sr25519Keyring::Bob;

		let alice_account: AccountId = alice_keyring.public().into();
		let bob_account: AccountId = bob_keyring.public().into();

		let tx_fee: Balance = (2 * TX_FEE).into(); // base tx fee + weight fee
		let alice_balance = force_set_balance(alice_account.clone());
		let bob_balance = force_set_balance(bob_account.clone());

		// Alice builds a meta transaction.

		let remark_call =
			RuntimeCall::System(frame_system::Call::remark_with_event { remark: vec![1] });

		let meta_tx_bare_ext = create_meta_tx_bare_ext(alice_account.clone());
		let meta_tx_sig =
			create_signature(remark_call.clone(), meta_tx_bare_ext.clone(), alice_keyring);
		let meta_tx_ext = (
			VerifySignatureExt::new_with_signature(meta_tx_sig, alice_account.clone()),
			// append signed part.
			meta_tx_bare_ext,
		);

		let meta_tx = MetaTxFor::<Runtime>::new(remark_call, META_EXTENSION_VERSION, meta_tx_ext);

		// Encode and share with the world.
		let meta_tx_encoded = meta_tx.encode();

		// Bob acts as meta transaction relayer.

		let meta_tx = MetaTxFor::<Runtime>::decode(&mut &meta_tx_encoded[..]).unwrap();
		let call = RuntimeCall::MetaTx(Call::dispatch {
			meta_tx: Box::new(meta_tx.clone()),
			meta_tx_encoded_len: meta_tx.encoded_size() as u32,
		});
		let tx_bare_ext = create_tx_bare_ext(bob_account.clone());
		let tx_sig = create_signature(call.clone(), tx_bare_ext.clone(), bob_keyring);
		let tx_ext = (
			VerifySignatureExt::new_with_signature(tx_sig, bob_account.clone()),
			// append signed part
			tx_bare_ext,
		);

		let uxt = UncheckedExtrinsic::new_transaction(call, tx_ext);

		// increment alice's nonce to invalidate the meta tx and verify that the
		// meta tx extension works.
		frame_system::Pallet::<Runtime>::inc_account_nonce(alice_account.clone());

		// Check Extrinsic validity and apply it.
		let result = apply_extrinsic(uxt);

		// Asserting the results.
		assert_eq!(result.unwrap_err().error, Error::<Runtime>::Stale.into());

		// Alice balance is unchanged, Bob paid the transaction fee.
		assert_eq!(alice_balance, Balances::free_balance(alice_account));
		assert_eq!(bob_balance - tx_fee, Balances::free_balance(bob_account));
	});
```

**File:** substrate/frame/meta-tx/src/mock.rs (L78-86)
```rust
	pub type MetaTxBareExtension = (
		MetaTxMarker<Runtime>,
		frame_system::CheckNonZeroSender<Runtime>,
		frame_system::CheckSpecVersion<Runtime>,
		frame_system::CheckTxVersion<Runtime>,
		frame_system::CheckGenesis<Runtime>,
		frame_system::CheckMortality<Runtime>,
		frame_system::CheckNonce<Runtime>,
	);
```

**File:** substrate/frame/system/src/extensions/check_nonce.rs (L70-91)
```rust
	pub fn validate_nonce_for_account(
		who: &T::AccountId,
		nonce: T::Nonce,
	) -> Result<ValidNonceInfo, TransactionValidityError> {
		let account = crate::Account::<T>::get(who);
		if account.providers.is_zero() && account.sufficients.is_zero() {
			// Nonce storage not paid for
			return Err(InvalidTransaction::Payment.into());
		}
		if nonce < account.nonce {
			return Err(InvalidTransaction::Stale.into());
		}

		let provides = vec![Encode::encode(&(who.clone(), nonce))];
		let requires = if account.nonce < nonce {
			vec![Encode::encode(&(who.clone(), nonce.saturating_sub(One::one())))]
		} else {
			vec![]
		};

		Ok(ValidNonceInfo { provides, requires })
	}
```

**File:** substrate/frame/system/src/extensions/check_nonce.rs (L93-105)
```rust
	/// In transaction extension, prepare nonce for account.
	pub fn prepare_nonce_for_account(
		who: &T::AccountId,
		mut nonce: T::Nonce,
	) -> Result<(), TransactionValidityError> {
		let account = crate::Account::<T>::get(who);
		if nonce > account.nonce {
			return Err(InvalidTransaction::Future.into());
		}
		nonce = nonce.checked_add(&T::Nonce::one()).unwrap_or(T::Nonce::zero());
		crate::Account::<T>::mutate(who, |account| account.nonce = nonce);
		Ok(())
	}
```
