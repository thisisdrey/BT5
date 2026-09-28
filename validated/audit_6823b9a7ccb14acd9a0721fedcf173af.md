### Title
Unconsented dust `transfer`/`_mint` calls `addToDepositTokensOfAccount` and can permanently cap a recipient's token list, DoS-ing deposits/withdrawals once `MAX_TOKENS_PER_USER` is reached — (File: contracts/DepositToken.sol)

### Summary

`Pool` enforces `MAX_TOKENS_PER_USER = 30` on the combined size of `depositTokensOfAccount` + `debtTokensOfAccount` via the `onlyIfAdditionWillNotReachMaxTokens` modifier. `DepositToken._transfer` and `DepositToken._mint` unconditionally call `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance transitions 0 → >0, with no opt-in from the recipient. An unprivileged attacker can therefore dust-transfer every pool deposit token to a chosen victim address (or, more severely, to the `feeCollector`), filling its set to the cap. Afterwards, any first-time receipt of a new deposit-token type by that account reverts with `UserReachedMaxTokens`, so the whole enclosing operation (deposit, withdraw, seize, transfer) reverts.

### Finding Description [1](#0-0) 

`addToDepositTokensOfAccount` reverts once the account holds 30 tokens across both sets. [1](#0-0) [2](#0-1) 

`_transfer` registers the token on the recipient whenever `amount_ > 0` and their prior balance was 0 — no allowance or recipient consent is required. [2](#0-1) [3](#0-2) 

The same 0 → >0 hook fires in `_mint` for `deposit()` (`_mint(onBehalfOf_, ...)`) and for fee mints to `feeCollector`. [3](#0-2) [4](#0-3) 

`transfer` is a public entry point; `_revertIfLocked` checks only the sender's unlocked balance, so dust transfers to arbitrary recipients are freely allowed. [4](#0-3) 

Attack paths:

1. **Protocol-wide withdrawal/deposit freeze via `feeCollector`.** `_withdraw` sends the fee share to `feeCollector` via `_transfer(account_, _pool.feeCollector(), _fee)`, and `deposit` mints the fee share via `_mint(_pool.feeCollector(), _fee)`. If the attacker fills `feeCollector`'s list to 30 with dust transfers of existing tokens, then the first deposit or withdrawal of *any additional* deposit-token type (one `feeCollector` does not yet hold) reverts, freezing those flows for all users until `feeCollector` empties a slot. This applies whenever `depositFee`/`withdrawFee > 0` on the deployed configuration.
2. **Per-victim griefing.** Fill a victim's 30 slots with dust; the victim cannot deposit a new collateral type, receive a new deposit-token transfer, or be the `onBehalfOf_` of a new deposit, and `seize` to them during liquidation fails. They can self-recover by transferring dust out, making this a temporary freeze.

This mirrors CVE-2017-13731's class — an attacker-controlled code path triggers a forced fault/revert (here, an unhandled set-cap revert) that produces a denial of service rather than memory corruption.

### Impact Explanation

- When fees are non-zero, filling `feeCollector` to the cap bricks `deposit()`/`withdraw()`/`transfer` on every deposit token the fee collector does not already hold, i.e., a temporary protocol-wide liveness failure on those assets until the fee collector contract/EOA clears slots (transferring dust out removes entries via `removeFromDepositTokensOfAccount`).
- For a targeted victim, it temporarily freezes their ability to onboard new collateral or receive new position tokens, including blocking liquidation `seize` transfers to them.
- Impact is capped at temporary freezing of funds; no direct theft or insolvency. Recovery requires the victim/fee collector to transfer dust tokens back out.

### Likelihood Explanation

- Cost is low: the attacker needs dust balances of up to `MAX_TOKENS_PER_USER` distinct pool deposit tokens, obtainable cheaply via AMM buys + `deposit()` or existing transfers.
- Requires no privileged role, no oracle manipulation, and works through plain public `transfer` calls (or via `Operator.execute` since `_msgSender()` resolves the real sender).
- Mitigations: `whenNotPaused`/`nonReentrant` don't apply; the only built-in counter is that `depositTokens` itself is capped at 30, so the set cannot grow beyond the cap — meaning the attack is always *executable* but its impact is bounded by how many slots are already legitimately used. If a victim already holds ≥ N token types, fewer dust transfers are needed.

### Recommendation

- Do not revert the parent operation when the recipient's set is full; instead, skip tracking (the set is only an enumeration used by `debtPositionOf`/`depositOf`/`debtOf` loops — alternatively iterate the global `depositTokens` set and read balances directly).
- Alternatively, exempt `feeCollector` (and other protocol addresses) from `addToDepositTokensOfAccount`, since fee balances are tracked elsewhere, or make registration of new tokens permissioned to deposit flows only (`_mint`) and not open `transfer`.
- Add a `claim`-style opt-in or a separate unbounded fee accounting path so fee transfers cannot fail on the 30-token cap.

### Proof of Concept

Hardhat sketch (per-victim path; feeCollector path identical with `to = pool.feeCollector()`):

```ts
// victim starts with 0 deposit-token positions
const MAX = (await pool.MAX_TOKENS_PER_USER()).toNumber(); // 30

// attacker acquires dust of deposit token i and transfers 1 wei to victim
for (let i = 0; i < MAX; ++i) {
  await depositToken[i].connect(attacker).deposit(10n, attacker.address);
  await depositToken[i].connect(attacker).transfer(victim.address, 1n);
}
expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.length(MAX);

// victim tries to deposit a collateral type they don't yet hold -> reverts
await underlying.approve(newDepositToken.address, amount);
await expect(
  newDepositToken.connect(victim).deposit(amount, victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// feeCollector variant:
// for each i: depositToken[i].transfer(feeCollector, 1)
// then any withdraw() on a token feeCollector doesn't hold reverts inside
// _withdraw -> _transfer(account_, feeCollector, _fee) -> addToDepositTokensOfAccount
```

Uncertainty note: the exact impact on `withdraw`/`deposit` depends on the deployed `depositFee`/`withdrawFee` being non-zero (fee path must execute) and on how many token types `feeCollector` already legitimately holds; on chains where fees are zero the per-victim variant still applies but the protocol-wide variant does not.

### Citations

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
```

**File:** contracts/DepositToken.sol (L348-353)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
```

**File:** contracts/DepositToken.sol (L485-488)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L517-525)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```
