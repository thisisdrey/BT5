Looking at the codebase, I need to trace the zero-value invariant through the confidential asset transfer path, specifically checking whether `confidential_transfer` has the same protection that `deposit` has.### Title
Zero-Amount Confidential Transfer Griefs Victim's `transfers_received` Counter, Blocking Incoming Transfers - (`aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move`)

---

### Summary

`confidential_transfer()` does not reject zero-amount transfers. Because the transfer amount is hidden inside a ZK proof, the contract cannot inspect it directly — but the ZK proof system (Bulletproofs range proof + sigma protocol) accepts `v = 0` as a valid witness. Every call unconditionally increments the recipient's `transfers_received` counter. An unprivileged attacker with zero balance can send 65,536 zero-value confidential transfers to any victim, saturating `MAX_TRANSFERS_BEFORE_ROLLOVER` and blocking the victim from receiving any further transfers until they perform a costly rollover + normalization cycle. The attacker can repeat this indefinitely.

---

### Finding Description

`deposit()` explicitly guards against zero-amount deposits:

```move
assert!(amount != 0, error::invalid_argument(E_POINTLESSLY_DEPOSITING_ZERO));
```

The error constant's own comment explains the reason:
> "Pointlessly depositing zero into one's confidential balance would unnecessarily increment the `transfers_received` counter." [1](#0-0) [2](#0-1) 

`confidential_transfer()` has no equivalent guard. After proof verification it unconditionally executes:

```move
recip_ca_store.transfers_received += 1;
assert!(
    recip_ca_store.transfers_received <= MAX_TRANSFERS_BEFORE_ROLLOVER,
    error::invalid_state(E_PENDING_BALANCE_MUST_BE_ROLLED_OVER)
);
``` [3](#0-2) 

`MAX_TRANSFERS_BEFORE_ROLLOVER` is 65,536. [4](#0-3) 

**Why a zero-amount proof is valid cryptographically:**

`assert_valid_transfer_proof` verifies two things:

1. A Bulletproofs batch range proof that each chunk of the transfer amount `v` lies in `[0, 2^16)`. Zero satisfies this — `0 ∈ [0, 65536)`.
2. A sigma-protocol proof of the NP relation `R^-_txfer`, which requires `old_balance = new_balance + v`. For `v = 0` and `old_balance = 0` (attacker registers with zero balance), `new_balance = 0` satisfies the relation exactly. [5](#0-4) [6](#0-5) 

The entry point `confidential_transfer_raw` is a `public entry fun`, reachable by any unprivileged transaction. [7](#0-6) 

**Rollover does not fully reset the victim's state.** After `rollover_pending_balance` runs, `normalized` is set to `false`:

```move
ca_store.normalized = false;
ca_store.transfers_received = 0;
ca_store.pending_balance = new_zero_pending_compressed();
``` [8](#0-7) 

The next rollover requires `normalized == true`:

```move
assert!(ca_store.normalized, error::invalid_state(E_NORMALIZATION_REQUIRED));
``` [9](#0-8) 

So the victim must also call `normalize_raw` (which requires generating a fresh ZK proof off-chain) before they can rollover again.

---

### Impact Explanation

**Attack loop:**

1. Attacker registers a confidential store with zero balance (valid — registration only requires a proof of knowledge of the decryption key).
2. Attacker sends 65,536 zero-amount `confidential_transfer_raw` transactions to the victim. Each passes all ZK checks and increments `transfers_received`.
3. Victim's `transfers_received` reaches 65,536. Any further incoming transfer (from any sender) aborts with `E_PENDING_BALANCE_MUST_BE_ROLLED_OVER`.
4. Victim must call `rollover_pending_balance` — which adds zero to their available balance (all 65,536 transfers were zero-value).
5. After rollover, `normalized = false`. Victim must generate a ZK normalization proof off-chain and submit `normalize_raw` before the next rollover is possible.
6. Attacker immediately repeats from step 2.

**Concrete harms:**
- Victim's confidential store is repeatedly wedged: they cannot receive any confidential transfers while `transfers_received == MAX_TRANSFERS_BEFORE_ROLLOVER`.
- Each cycle forces the victim to pay gas for rollover + normalization and to perform expensive off-chain ZK proof generation.
- The attacker's cost is only gas for 65,536 transactions per cycle; no actual asset balance is required.
- A targeted victim (e.g., a confidential-asset exchange or payment hub) can be continuously denied incoming transfers.

---

### Likelihood Explanation

The attack is fully unprivileged: any registered account can call `confidential_transfer_raw`. Constructing a valid ZK proof for `v = 0` is straightforward — it is a degenerate case of the normal proof generation path. The attacker needs no on-chain assets beyond gas. The victim has no way to prevent or filter incoming zero-amount transfers.

---

### Recommendation

Mirror the guard already present in `deposit()` inside `confidential_transfer()`. Because the amount is hidden, the check must be applied to the decrypted/plaintext amount before proof construction on the client side, and enforced on-chain by rejecting transfers whose encrypted amount commitment is the identity point (i.e., the Pedersen commitment to 0 with randomness 0). A simpler and sufficient mitigation is to add a non-zero-amount check analogous to the deposit guard:

In `confidential_transfer()`, after `assert_valid_transfer_proof` returns the `amount` (a `Balance<Pending>`), verify that it is not the zero balance before incrementing `transfers_received`. Alternatively, add a dedicated error constant `E_POINTLESSLY_TRANSFERRING_ZERO` and abort if the recovered pending balance is the identity.

A complementary defense is to require a minimum transfer amount enforced at the protocol level (e.g., at least 1 unit), which would also prevent dust-spam attacks.

---

### Proof of Concept

**Textual PoC:**

1. Alice registers a confidential store for APT with zero balance.
2. Bob registers a confidential store for APT and deposits 1,000 APT.
3. Alice constructs 65,536 valid `TransferProof` objects each with `v = 0` (all amount chunks are 0, range proof for 0 is valid, sigma proof verifies `0 = 0 + 0`).
4. Alice submits 65,536 `confidential_transfer_raw` transactions targeting Bob.
5. Bob's `transfers_received` reaches 65,536.
6. Any subsequent `confidential_transfer_raw` or `deposit` targeting Bob aborts with `E_PENDING_BALANCE_MUST_BE_ROLLED_OVER`.
7. Bob calls `rollover_pending_balance` — his available balance is unchanged (zero was added).
8. Bob's `normalized` is now `false`. Bob must generate a normalization proof and call `normalize_raw` before he can rollover again.
9. Alice immediately sends another 65,536 zero-amount transfers. Repeat indefinitely.

**Key invariant violated:** `transfers_received` is supposed to count meaningful incoming value increments that grow the pending balance chunks. Zero-amount transfers increment the counter without growing the balance, exhausting the rollover budget for free. [10](#0-9) [3](#0-2)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L87-88)
```text
    /// Pointlessly depositing zero into one's confidential balance would unncessarily increment the `transfers_received` counter.
    const E_POINTLESSLY_DEPOSITING_ZERO: u64 = 18;
```

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L107-109)
```text
    /// The maximum number of transactions can be aggregated on the pending balance before rollover is required.
    /// i.e., `ConfidentialStore::transfers_received` will never exceed this value.
    const MAX_TRANSFERS_BEFORE_ROLLOVER: u64 = 65536;
```

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L184-202)
```text
    /// Per-(user, asset-type) encrypted balance store (confidential variant of `FungibleStore`).
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

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L461-462)
```text
        assert!(!incoming_transfers_paused(addr, asset_type), error::invalid_state(E_INCOMING_TRANSFERS_PAUSED));
        assert!(amount != 0, error::invalid_argument(E_POINTLESSLY_DEPOSITING_ZERO));
```

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L566-613)
```text
    /// Deserializes cryptographic data and forwards to `confidential_transfer`.
    public entry fun confidential_transfer_raw(
        sender: &signer,
        asset_type: Object<fungible_asset::Metadata>,
        to: address,
        new_balance_P: vector<vector<u8>>,
        new_balance_R: vector<vector<u8>>,
        new_balance_R_eff_aud: vector<vector<u8>>, // new balance R component for the *effective* auditor only
        amount_P: vector<vector<u8>>,
        amount_R_sender: vector<vector<u8>>,
        amount_R_recip: vector<vector<u8>>,
        amount_R_eff_aud: vector<vector<u8>>, // amount R components for the *effective* auditor only
        ek_volun_auds: vector<vector<u8>>, // contains EKs for the *voluntary* auditors only
        amount_R_volun_auds: vector<vector<vector<u8>>>, // amount R components for the *voluntary* auditors only
        zkrp_new_balance: vector<u8>,
        zkrp_amount: vector<u8>,
        sigma_proto_comm: vector<vector<u8>>,
        sigma_proto_resp: vector<vector<u8>>,
        memo: vector<u8>,
    ) acquires ConfidentialStore, AssetConfig, GlobalConfig {
        let compressed_new_balance = new_compressed_available_from_bytes(new_balance_P, new_balance_R, new_balance_R_eff_aud);

        let compressed_amount = confidential_amount::new_compressed_from_bytes(
            amount_P, amount_R_sender, amount_R_recip, amount_R_eff_aud, amount_R_volun_auds,
        );

        let compressed_ek_volun_auds = ek_volun_auds.map(|bytes| {
            new_compressed_point_from_bytes(bytes).extract()
        });

        let zkrp_new_balance = bulletproofs::range_proof_from_bytes(zkrp_new_balance);
        let zkrp_amount = bulletproofs::range_proof_from_bytes(zkrp_amount);
        let sigma = sigma_protocol_proof::new_proof_from_bytes(sigma_proto_comm, sigma_proto_resp);
        let proof = TransferProof::V1 {
            compressed_new_balance,
            compressed_amount,
            compressed_ek_volun_auds,
            zkrp_new_balance, zkrp_amount, sigma
        };

        confidential_transfer(
            sender,
            asset_type,
            to,
            proof,
            memo,
        )
    }
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

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L777-778)
```text
        assert!(ca_store.normalized, error::invalid_state(E_NORMALIZATION_REQUIRED));
        assert!(ca_store.transfers_received > 0, error::invalid_state(E_NOTHING_TO_ROLLOVER));
```

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L784-786)
```text
        ca_store.normalized = false;
        ca_store.transfers_received = 0;
        ca_store.pending_balance = new_zero_pending_compressed();
```

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L1335-1384)
```text
    /// Verifies range proofs + $\Sigma$-protocol for transfer. Returns (new_balance, recipient_pending).
    fun assert_valid_transfer_proof(
        sender: &signer,
        recipient_addr: address,
        asset_type: Object<fungible_asset::Metadata>,
        compressed_ek_sender: &CompressedRistretto,
        compressed_ek_recip: &CompressedRistretto,
        compressed_old_balance: &CompressedBalance<Available>,
        compressed_ek_eff_aud: &Option<CompressedRistretto>,
        proof: TransferProof
    ): (
        CompressedBalance<Available>,
        Balance<Pending>,
        CompressedAmount,
        vector<CompressedRistretto>,
    ) {

        let TransferProof::V1 {
            compressed_new_balance, compressed_amount,
            compressed_ek_volun_auds,
            zkrp_new_balance, zkrp_amount, sigma
        } = proof;

        // Note: `update_auditor` already guarantees that `compressed_ek_eff_aud` is not the identity, but the voluntary
        // auditor EKs need to be manually checked.
        compressed_ek_volun_auds.for_each_ref(|ek| {
            assert!(!ek.is_identity(), error::invalid_argument(E_EK_IS_IDENTITY));
        });

        let has_effective_auditor = compressed_ek_eff_aud.is_some();
        let num_volun_auditors = compressed_ek_volun_auds.length();

        // Auditor count checks are performed inside new_transfer_statement
        let (stmt, amount) = sigma_protocol_transfer::new_transfer_statement(
            *compressed_ek_sender, *compressed_ek_recip,
            compressed_old_balance, &compressed_new_balance,
            &compressed_amount,
            compressed_ek_eff_aud, &compressed_ek_volun_auds,
        );

        confidential_range_proofs::assert_valid_range_proof(compressed_amount.get_compressed_P(), &zkrp_amount);
        confidential_range_proofs::assert_valid_range_proof(compressed_new_balance.get_compressed_P(), &zkrp_new_balance);

        let session = sigma_protocol_transfer::new_session(
            sender, recipient_addr, asset_type, has_effective_auditor, num_volun_auditors,
        );
        session.assert_verifies(&stmt, &sigma);

        (compressed_new_balance, amount, compressed_amount, compressed_ek_volun_auds)
    }
```

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_range_proofs.move (L35-51)
```text
    /// Asserts that the given commitment chunks are each in [0, 2^16) via a range proof.
    public(friend) fun assert_valid_range_proof(
        commitments: &vector<CompressedRistretto>,
        zkrp: &RangeProof
    ) {
        assert!(
            verify_batch_range_proof(
                commitments,
                &ristretto255::basepoint(),
                &ristretto255::hash_to_point_base(),
                zkrp,
                confidential_balance::get_chunk_size_bits(),
                BULLETPROOFS_DST
            ),
            error::out_of_range(ERANGE_PROOF_VERIFICATION_FAILED)
        );
    }
```
