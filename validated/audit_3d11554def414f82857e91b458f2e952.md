### Title
Missing Minimum-Amount Guard in `confidential_transfer()` Allows Spam of Recipient's `transfers_received` Counter, Blocking Incoming Confidential Transfers — (`aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move`)

---

### Summary

`confidential_transfer()` in the Confidential Asset module does not enforce a minimum transfer amount, while the sibling `deposit()` function explicitly rejects zero-amount calls. Any registered user can craft a cryptographically valid 0-amount transfer proof and submit it repeatedly, incrementing the recipient's `transfers_received` counter at the cost of gas only. Once the counter reaches `MAX_TRANSFERS_BEFORE_ROLLOVER` (65 536), every subsequent inbound confidential transfer or deposit to that address aborts with `E_PENDING_BALANCE_MUST_BE_ROLLED_OVER`, effectively denying the victim the ability to receive confidential funds until they perform a rollover — which, after the first cycle, additionally requires a ZK-proof-bearing normalize call.

---

### Finding Description

**`deposit()` guards against zero; `confidential_transfer()` does not.**

`deposit()` contains an explicit guard:

```move
assert!(amount != 0, error::invalid_argument(E_POINTLESSLY_DEPOSITING_ZERO));
``` [1](#0-0) 

`confidential_transfer()` has no equivalent check. It accepts any proof that satisfies the range proof and sigma protocol, both of which are valid for a 0-amount transfer (all Pedersen commitment chunks are `0·G + r·H`, which is in `[0, 2^16)` and satisfies the balance-conservation relation `new_balance = old_balance − 0`):

```move
public(friend) fun confidential_transfer(
    sender: &signer,
    asset_type: Object<fungible_asset::Metadata>,
    to: address,
    proof: TransferProof,
    memo: vector<u8>,
) acquires ConfidentialStore, AssetConfig, GlobalConfig {
    // ... no minimum-amount check ...
    let (compressed_new_balance, amount, compressed_amount, ek_volun_auds) =
        assert_valid_transfer_proof(..., proof);

    let recip_ca_store = borrow_confidential_store_mut(to, asset_type);
    let new_pending_balance = add_assign_pending(&mut recip_ca_store.pending_balance, &amount);
    recip_ca_store.transfers_received += 1;          // ← incremented unconditionally

    assert!(
        recip_ca_store.transfers_received <= MAX_TRANSFERS_BEFORE_ROLLOVER,
        error::invalid_state(E_PENDING_BALANCE_MUST_BE_ROLLED_OVER)
    );
``` [2](#0-1) 

`MAX_TRANSFERS_BEFORE_ROLLOVER` is 65 536: [3](#0-2) 

**Rollover state machine makes repeated attacks progressively more expensive for the victim.**

`register()` initialises `normalized: true`: [4](#0-3) 

`rollover_pending_balance()` requires `normalized == true` and resets it to `false`: [5](#0-4) 

After the first rollover `normalized` is `false`. To rollover again the victim must first call `normalize_raw()` (a ZK-proof-bearing transaction) to set `normalized = true`. The attacker can re-spam immediately after each rollover, forcing the victim into a continuous normalize → rollover cycle.

---

### Impact Explanation

An unprivileged attacker who holds a registered `ConfidentialStore` for the targeted asset type can:

1. Pre-compute 65 536 valid 0-amount `TransferProof` objects (off-chain, no token cost).
2. Submit 65 536 `confidential_transfer_raw` transactions to the victim's address.
3. The victim's `transfers_received` counter reaches 65 536.
4. All subsequent `confidential_transfer` and `deposit` calls to the victim abort with `E_PENDING_BALANCE_MUST_BE_ROLLED_OVER`.
5. The victim must rollover (and, after the first cycle, also normalize with a ZK proof) before they can receive funds again.
6. The attacker repeats immediately, maintaining the denial of service.

The victim's confidential fungible-asset balances are not stolen, but their ability to receive confidential transfers — the core user-facing function of the Confidential Asset protocol — is durably disrupted at attacker cost of gas only.

---

### Likelihood Explanation

- The entry point `confidential_transfer_raw` is a public, permissionless entry function callable by any registered user.
- A 0-amount transfer proof is trivially constructable: the witness is `(dk, r_new_balance, r_amount = 0)` with `r_amount` chosen freely; the range proof for the all-zero amount vector is a standard Bulletproof for value 0.
- The attacker spends no tokens — only gas — making the attack economically rational against high-value targets.
- The victim has no on-chain mechanism to prevent the attack before it completes a full 65 536-transaction wave; `set_incoming_transfers_paused` requires the victim to act first, but the attacker can front-run or simply complete the wave before the victim reacts.

---

### Recommendation

Add a non-zero amount assertion at the top of `confidential_transfer()`, mirroring the guard already present in `deposit()`:

```move
// In confidential_transfer(), after the self-transfer check:
assert!(
    !amount_is_zero(&proof),   // or check after proof extraction
    error::invalid_argument(E_POINTLESSLY_TRANSFERRING_ZERO)
);
```

Because the amount is encrypted, the cleanest enforcement point is **after** `assert_valid_transfer_proof` returns the decoded `amount` (a `Balance<Pending>` value), by asserting it is not the zero balance before writing to the recipient's store. This mirrors the pattern used in `deposit()` and closes the spam vector at zero additional cryptographic cost.

---

### Proof of Concept

**Setup:**
- Attacker registers a `ConfidentialStore` for APT (or any allowed asset type).
- Victim registers a `ConfidentialStore` for the same asset type; initial state: `normalized = true`, `transfers_received = 0`.

**Attack loop (65 536 iterations):**
```
for i in 0..65536:
    proof = generate_transfer_proof(
        sender_dk   = attacker_dk,
        old_balance = attacker_balance,   // unchanged each iteration (amount = 0)
        new_balance = attacker_balance,   // same as old
        amount      = 0,
        recip_ek    = victim_ek,
    )
    submit confidential_transfer_raw(attacker, asset_type, victim_addr, proof, ...)
```

After 65 536 transactions, `victim.transfers_received == 65536`.

**Verification:**
Any subsequent `confidential_transfer` or `deposit` targeting the victim aborts:
```
E_PENDING_BALANCE_MUST_BE_ROLLED_OVER (error code 6)
```

The victim must call `rollover_pending_balance()` (which succeeds because `normalized` is still `true` from registration). After rollover, `normalized = false`. The attacker immediately re-runs the loop. Now the victim must also call `normalize_raw()` (ZK proof required) before the next rollover, creating a sustained, escalating denial-of-service.

### Citations

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L109-109)
```text
    const MAX_TRANSFERS_BEFORE_ROLLOVER: u64 = 65536;
```

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L436-444)
```text
        let ca_store = ConfidentialStore::V1 {
            pause_incoming: false,
            normalized: true,
            transfers_received: 0,
            pending_balance: new_zero_pending_compressed(),
            available_balance: new_zero_available_compressed(),
            ek,
            auditor_hint: std::option::none() // balance == 0 is publicly-known ==> auditor ciphertext is left empty
        };
```

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L462-462)
```text
        assert!(amount != 0, error::invalid_argument(E_POINTLESSLY_DEPOSITING_ZERO));
```

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L616-653)
```text
    public(friend) fun confidential_transfer(
        sender: &signer,
        asset_type: Object<fungible_asset::Metadata>,
        to: address,
        proof: TransferProof,
        memo: vector<u8>,
    ) acquires ConfidentialStore, AssetConfig, GlobalConfig {
        assert!(!is_emergency_paused(), error::invalid_state(E_EMERGENCY_PAUSED));
        assert!(is_safe_for_confidentiality(&asset_type), error::invalid_argument(E_UNSAFE_DISPATCHABLE_FA));
        assert!(is_confidentiality_enabled_for_asset_type(asset_type), error::invalid_argument(E_ASSET_TYPE_DISALLOWED));
        assert!(!incoming_transfers_paused(to, asset_type), error::invalid_state(E_INCOMING_TRANSFERS_PAUSED));
        assert!(memo.length() <= MAX_MEMO_BYTES, error::invalid_argument(E_MEMO_TOO_LONG));

        let from = signer::address_of(sender);
        assert!(from != to, error::invalid_argument(E_SELF_TRANSFER));
        let effective_auditor = get_effective_auditor_config(asset_type);
        let ek_sender = get_encryption_key(from, asset_type);
        let ek_recip = get_encryption_key(to, asset_type);
        let old_balance = get_available_balance(from, asset_type);

        // Note: Sender's amount in `TransferProof::compressed_amount::compressed_R_sender` is not used here; only included so it can be indexed for dapps that need it
        let (compressed_new_balance, amount, compressed_amount, ek_volun_auds) =
            assert_valid_transfer_proof(
                sender, to, asset_type,
                &ek_sender, &ek_recip,
                &old_balance, &effective_auditor.config.ek,
                proof
            );

        // Update recipient's confidential store
        let recip_ca_store = borrow_confidential_store_mut(to, asset_type);
        let new_pending_balance = add_assign_pending(&mut recip_ca_store.pending_balance, &amount);
        recip_ca_store.transfers_received += 1;

        assert!(
            recip_ca_store.transfers_received <= MAX_TRANSFERS_BEFORE_ROLLOVER,
            error::invalid_state(E_PENDING_BALANCE_MUST_BE_ROLLED_OVER)
        );
```

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L777-786)
```text
        assert!(ca_store.normalized, error::invalid_state(E_NORMALIZATION_REQUIRED));
        assert!(ca_store.transfers_received > 0, error::invalid_state(E_NOTHING_TO_ROLLOVER));

        ca_store.available_balance.add_assign_available_excluding_auditor(&ca_store.pending_balance);
        // Note: R_aud components [must] remain stale, but will be refreshed on the next normalize/withdraw/transfer
        // Note: Since this function does not update the *auditor's* available balance, we do not update the auditor hint.

        ca_store.normalized = false;
        ca_store.transfers_received = 0;
        ca_store.pending_balance = new_zero_pending_compressed();
```
