### Title
Unprivileged Griefing via `confidential_transfer` Fills Victim's `transfers_received` Counter, Permanently Blocking Incoming Transfers Until Expensive ZK-Proof Recovery — (`aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move`)

---

### Summary

The `confidential_transfer` and `deposit` entry functions in the Confidential Asset module allow any unprivileged actor to increment a victim's `transfers_received` counter up to `MAX_TRANSFERS_BEFORE_ROLLOVER` (65,536). Once the counter is saturated, all further incoming confidential transfers to the victim abort with `E_PENDING_BALANCE_MUST_BE_ROLLED_OVER`. Recovery requires the victim to generate and submit an expensive off-chain ZK proof (`normalize_raw`) before they can call `rollover_pending_balance`. An attacker can repeat this cycle continuously, forcing the victim to repeatedly bear the computational and gas cost of ZK-proof generation to keep their account usable for receiving transfers.

---

### Finding Description

**Root cause — same class as the external report:** The external bug allows a malicious actor to fill a sequential deposit/withdrawal queue at low cost, blocking legitimate users. The Aptos analog is the `transfers_received` counter in `ConfidentialStore`, which any sender can increment by sending confidential transfers to a victim's address.

**Relevant state:**

```
ConfidentialStore::V1 {
    normalized: bool,           // must be true before rollover
    transfers_received: u64,    // incremented on every incoming transfer/deposit
    pending_balance: ...,
    ...
}
```

`MAX_TRANSFERS_BEFORE_ROLLOVER = 65536`

**Attack path in `confidential_transfer`:**

```move
// Update recipient's confidential store
let recip_ca_store = borrow_confidential_store_mut(to, asset_type);
let new_pending_balance = add_assign_pending(&mut recip_ca_store.pending_balance, &amount);
recip_ca_store.transfers_received += 1;          // ← attacker increments this

assert!(
    recip_ca_store.transfers_received <= MAX_TRANSFERS_BEFORE_ROLLOVER,
    error::invalid_state(E_PENDING_BALANCE_MUST_BE_ROLLED_OVER)
);
```

The counter is incremented **before** the guard check, and there is no minimum transfer amount enforced in `confidential_transfer`. An attacker who has registered a confidential store and holds any non-zero balance can send 65,536 transfers (each for the minimum provable amount) to a victim's address, saturating the counter.

**Recovery is expensive:** After saturation, the victim must:

1. Call `normalize_raw` — a withdrawal with `amount = 0` that requires generating a fresh Bulletproofs range proof + Σ-protocol proof off-chain (computationally expensive, requires knowledge of the decryption key and current balance ciphertext).
2. Only after `normalized = true` can the victim call `rollover_pending_balance` to reset `transfers_received = 0`.

The attacker can immediately re-saturate the counter after each rollover, forcing the victim into an indefinite cycle of ZK-proof generation.

**`deposit` also increments the counter for the depositor's own account**, but this is self-inflicted and not the primary attack vector.

**Partial mitigation exists but is incomplete:** The victim can call `set_incoming_transfers_paused(true)` to block further incoming transfers. However:
- The victim must notice the attack before the counter is saturated.
- Pausing also blocks all legitimate incoming transfers, degrading usability.
- The victim still must normalize + rollover to reset the counter even after pausing.

---

### Impact Explanation

- **Temporary but repeatable denial of incoming confidential transfers** to any registered user.
- **Forced expensive ZK-proof generation** (normalize) on every recovery cycle — each normalize requires a Bulletproofs range proof and a Σ-protocol proof, which are computationally intensive off-chain operations.
- **Gas cost imposed on victim** for each normalize + rollover transaction pair.
- Victim's **existing available balance is not at risk** (they can still withdraw), but their ability to receive new confidential transfers is continuously disrupted.
- A high-value target (e.g., a confidential-asset-integrated DeFi contract or exchange address) that cannot easily pause incoming transfers is most severely affected.

---

### Likelihood Explanation

- Any registered confidential-asset user can execute the attack with no special privileges.
- The attacker must generate 65,536 ZK proofs and pay gas for 65,536 transactions — a significant but not prohibitive cost for a motivated attacker targeting a high-value address.
- The attack is repeatable with no cooldown after each victim rollover.
- On Aptos mainnet, gas fees are low, reducing the per-transaction cost.

---

### Recommendation

1. **Enforce a minimum transfer amount** in `confidential_transfer` (analogous to `minDepositAmount` in the external report) to raise the cost per spam transfer.
2. **Allow the victim to rollover without a prior normalize step** when `transfers_received` is at the limit — i.e., relax the `normalized == true` precondition in `rollover_pending_balance` when the counter is saturated, so recovery does not require a ZK proof.
3. **Rate-limit incoming transfers per sender per epoch** to prevent a single actor from saturating the counter in one burst.
4. **Document the `set_incoming_transfers_paused` mitigation prominently** so users and integrators know to monitor `transfers_received` and pause proactively.

---

### Proof of Concept

```
// Attacker (Alice) setup:
// 1. Alice registers a ConfidentialStore for asset_type with any non-zero balance.
// 2. Alice generates 65,536 valid TransferProofs, each transferring 1 unit to Bob.
// 3. Alice submits 65,536 confidential_transfer_raw transactions targeting Bob.
//    After each: Bob's transfers_received increments by 1.
// 4. After 65,536 transactions: Bob's transfers_received == 65536.
//    Any further confidential_transfer to Bob aborts with E_PENDING_BALANCE_MUST_BE_ROLLED_OVER.

// Bob's recovery (forced):
// 5. Bob must call normalize_raw (ZK proof required, expensive off-chain computation).
//    This sets Bob's normalized = true.
// 6. Bob calls rollover_pending_balance.
//    This resets transfers_received = 0, normalized = false.
// 7. Alice immediately repeats from step 2.
```

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L107-109)
```text
    /// The maximum number of transactions can be aggregated on the pending balance before rollover is required.
    /// i.e., `ConfidentialStore::transfers_received` will never exceed this value.
    const MAX_TRANSFERS_BEFORE_ROLLOVER: u64 = 65536;
```

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L185-202)
```text
    enum ConfidentialStore has key {
        V1 {
            /// Must be paused before key rotation to prevent mid-rotation pending balance changes.
            pause_incoming: bool,
            /// True if all available balance chunks are within 16-bit bounds (required before rollover).
            normalized: bool,
            /// Number of transfers received; upper-bounds pending balance chunk sizes.
            transfers_received: u64,
            /// Incoming transfers accumulate here; must be rolled over into `available_balance` to spend.
            pending_balance: CompressedBalance<Pending>,
            /// Spendable balance (8 chunks, 128-bit). R_aud components for auditor decryption (empty if no auditor).
            available_balance: CompressedBalance<Available>,
            /// User's encryption key for this asset type.
            ek: CompressedRistretto,
            /// Tracks which auditor the balance ciphertext is encrypted for: global/effective and epoch
            auditor_hint: Option<EffectiveAuditorHint>
        }
    }
```

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L477-485)
```text

        add_assign_pending(&mut ca_store.pending_balance, &new_pending_u64_no_randomness(amount));
        ca_store.transfers_received += 1;

        // Make sure the depositor has "room" in their pending balance for this deposit
        assert!(
            ca_store.transfers_received <= MAX_TRANSFERS_BEFORE_ROLLOVER,
            error::invalid_state(E_PENDING_BALANCE_MUST_BE_ROLLED_OVER)
        );
```

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L645-653)
```text
        // Update recipient's confidential store
        let recip_ca_store = borrow_confidential_store_mut(to, asset_type);
        let new_pending_balance = add_assign_pending(&mut recip_ca_store.pending_balance, &amount);
        recip_ca_store.transfers_received += 1;

        assert!(
            recip_ca_store.transfers_received <= MAX_TRANSFERS_BEFORE_ROLLOVER,
            error::invalid_state(E_PENDING_BALANCE_MUST_BE_ROLLED_OVER)
        );
```

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L767-789)
```text
    /// Rolls over pending balance into available balance, resetting pending to zero.
    public entry fun rollover_pending_balance(
        sender: &signer,
        asset_type: Object<fungible_asset::Metadata>
    ) acquires ConfidentialStore, GlobalConfig {
        assert!(!is_emergency_paused(), error::invalid_state(E_EMERGENCY_PAUSED));

        let user = signer::address_of(sender);
        let ca_store = borrow_confidential_store_mut(user, asset_type);

        assert!(ca_store.normalized, error::invalid_state(E_NORMALIZATION_REQUIRED));
        assert!(ca_store.transfers_received > 0, error::invalid_state(E_NOTHING_TO_ROLLOVER));

        ca_store.available_balance.add_assign_available_excluding_auditor(&ca_store.pending_balance);
        // Note: R_aud components [must] remain stale, but will be refreshed on the next normalize/withdraw/transfer
        // Note: Since this function does not update the *auditor's* available balance, we do not update the auditor hint.

        ca_store.normalized = false;
        ca_store.transfers_received = 0;
        ca_store.pending_balance = new_zero_pending_compressed();

        event::emit(RolledOver::V1 { addr: user, asset_type, new_available_balance: ca_store.available_balance });
    }
```
