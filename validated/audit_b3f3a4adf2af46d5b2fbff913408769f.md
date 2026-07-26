### Title
Single Unregistered Shareholder Permanently Blocks `distribute()` for All Vesting Contract Participants — (`aptos-move/framework/aptos-framework/sources/vesting.move`)

---

### Summary

`vesting.distribute()` iterates over every shareholder and calls `aptos_account::deposit_coins` for each one inside a single atomic loop. If any one deposit aborts, the entire transaction reverts and **no shareholder receives anything**. The `set_beneficiary()` function already carries an explicit guard against this (`assert_account_is_registered_for_apt`), but `create_vesting_contract()` applies no equivalent check on the initial shareholder list, leaving a reachable DoS path.

---

### Finding Description

`distribute()` loops over all shareholders and deposits their share of unlocked APT:

```move
// vesting.move lines 733-740
shareholders.for_each_ref(|shareholder| {
    let shareholder = *shareholder;
    let shares = pool_u64::shares(grant_pool, shareholder);
    let amount = pool_u64::shares_to_amount_with_total_coins(grant_pool, shares, total_distribution_amount);
    let share_of_coins = coin::extract(&mut coins, amount);
    let recipient_address = get_beneficiary(vesting_contract, shareholder);
    aptos_account::deposit_coins(recipient_address, share_of_coins);
});
```

`aptos_account::deposit_coins` aborts when the recipient account exists, is **not** registered for `AptosCoin`, and has explicitly opted out of direct coin transfers:

```move
// aptos_account.move lines 123-127
if (!coin::is_account_registered<CoinType>(to)) {
    assert!(
        can_receive_direct_coin_transfers(to),
        error::permission_denied(EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS)
    );
```

`create_vesting_contract()` checks that the **withdrawal address** is registered for APT, but performs **no such check on the shareholders themselves**:

```move
// vesting.move lines 549-558
assert_account_is_registered_for_apt(withdrawal_address);
assert!(shareholders.length() > 0, ...);
assert!(buy_ins.length() == shareholders.length(), ...);
// ← no assert_account_is_registered_for_apt per shareholder
```

The codebase itself acknowledges the risk in `set_beneficiary()`, which was patched with an explicit guard and comment:

```move
// vesting.move lines 921-923
// Verify that the beneficiary account is set up to receive APT. This is a requirement so distribute() wouldn't
// fail and block all other accounts from receiving APT if one beneficiary is not registered.
assert_account_is_registered_for_apt(new_beneficiary);
```

That guard was never back-ported to the initial shareholder list in `create_vesting_contract()`.

**Attack path:**

1. Admin creates a vesting contract; one shareholder address was created via the low-level `account::create_account()` (not `aptos_account::create_account()`), so it is **not** registered for `AptosCoin`.
2. That shareholder calls `aptos_account::set_allow_direct_coin_transfers(false)` — a fully unprivileged, permissionless transaction.
3. Every subsequent call to `distribute(contract_address)` aborts at that shareholder's iteration, reverting the entire transaction.
4. Because `terminate_vesting_contract()` calls `distribute()` internally, contract termination is also blocked.

---

### Impact Explanation

All shareholders in the affected vesting contract are permanently unable to receive their vested APT distributions. The `terminate_vesting_contract()` path is also wedged, preventing the admin from recovering the remaining grant. Funds remain locked in the staking pool until the admin calls `set_beneficiary()` or `reset_beneficiary()` to replace the blocking shareholder's recipient address — but the admin may not know which shareholder is the cause, and the fix requires a separate privileged transaction.

This matches the Frax M-07 root cause exactly: a loop over a fixed collection where one invalid element causes the entire operation to revert, making the function inoperable until a privileged actor manually removes or replaces the bad element.

---

### Likelihood Explanation

- Accounts created via `account::create_account()` (resource accounts, programmatically created accounts) are not automatically registered for APT.
- `set_allow_direct_coin_transfers(false)` is a standard, documented, permissionless entry function callable by any account holder.
- A shareholder who is a resource account or a multisig account may legitimately not be registered for APT at contract creation time.
- The inconsistency between `set_beneficiary()` (guarded) and `create_vesting_contract()` (unguarded) shows the risk was known but incompletely mitigated.

---

### Recommendation

Add the same `assert_account_is_registered_for_apt` check for every shareholder inside `create_vesting_contract()`, mirroring the existing guard in `set_beneficiary()`:

```move
shareholders.for_each_ref(|shareholder| {
    let shareholder: address = *shareholder;
    assert_account_is_registered_for_apt(shareholder);  // ← add this
    let (_, buy_in) = simple_map::remove(&mut buy_ins, &shareholder);
    ...
});
```

Alternatively, `distribute()` could skip shareholders whose deposit would fail (Move has no try/catch, so a pre-flight `can_receive_direct_coin_transfers` check before each deposit would suffice), but the creation-time guard is simpler and consistent with the existing `set_beneficiary()` pattern.

---

### Proof of Concept

1. Deploy a vesting contract with two shareholders: `alice` (normal account, registered for APT) and `bob` (created via `account::create_account()`, **not** registered for APT).
2. `bob` calls `aptos_account::set_allow_direct_coin_transfers(false)`.
3. After the lockup expires, call `distribute(contract_address)`.
4. The transaction aborts at `bob`'s iteration with `EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS`.
5. `alice` receives nothing. `terminate_vesting_contract()` also aborts.
6. The contract is stuck until the admin calls `set_beneficiary(admin, contract_address, bob, some_registered_address)` — which itself requires `some_registered_address` to be known and available.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L549-558)
```text
        assert!(
            !system_addresses::is_reserved_address(withdrawal_address),
            error::invalid_argument(EINVALID_WITHDRAWAL_ADDRESS),
        );
        assert_account_is_registered_for_apt(withdrawal_address);
        assert!(shareholders.length() > 0, error::invalid_argument(ENO_SHAREHOLDERS));
        assert!(
            buy_ins.length() == shareholders.length(),
            error::invalid_argument(ESHARES_LENGTH_MISMATCH),
        );
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L730-740)
```text
        // Distribute coins to all shareholders in the vesting contract.
        let grant_pool = &vesting_contract.grant_pool;
        let shareholders = &grant_pool.shareholders();
        shareholders.for_each_ref(|shareholder| {
            let shareholder = *shareholder;
            let shares = pool_u64::shares(grant_pool, shareholder);
            let amount = pool_u64::shares_to_amount_with_total_coins(grant_pool, shares, total_distribution_amount);
            let share_of_coins = coin::extract(&mut coins, amount);
            let recipient_address = get_beneficiary(vesting_contract, shareholder);
            aptos_account::deposit_coins(recipient_address, share_of_coins);
        });
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L771-775)
```text
    public entry fun terminate_vesting_contract(admin: &signer, contract_address: address) acquires VestingContract {
        assert_active_vesting_contract(contract_address);

        // Distribute all withdrawable coins, which should have been from previous rewards withdrawal or vest.
        distribute(contract_address);
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L920-923)
```text
    ) acquires VestingContract {
        // Verify that the beneficiary account is set up to receive APT. This is a requirement so distribute() wouldn't
        // fail and block all other accounts from receiving APT if one beneficiary is not registered.
        assert_account_is_registered_for_apt(new_beneficiary);
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L123-130)
```text
        if (!coin::is_account_registered<CoinType>(to)) {
            assert!(
                can_receive_direct_coin_transfers(to),
                error::permission_denied(EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS)
            );
            coin::register<CoinType>(&create_signer(to));
        };
        coin::deposit<CoinType>(to, coins)
```
