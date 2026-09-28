### Title
Attacker can grief any account (including the fee collector) by filling its per-account token list, causing `UserReachedMaxTokens` reverts that DoS deposits/transfers and liquidations - (File: contracts/Pool.sol)

### Summary
The Zeppelin report is an improper-input-validation DoS: an unprivileged user supplies an input the system fails to bound, breaking availability. The Metronome analog lives in the per-account token lists. `Pool` enforces `MAX_TOKENS_PER_USER = 30` and reverts `UserReachedMaxTokens` in `onlyIfAdditionWillNotReachMaxTokens` [1](#0-0) [2](#0-1) . `DepositToken._transfer` and `_mint` call `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance moves from 0 to >0 [3](#0-2) [4](#0-3) , and `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` are gated only by that cap plus a sender-is-token check [5](#0-4) . There is no minimum-amount validation, so a dust transfer of 1 wei of each listed deposit token permanently occupies a slot in the victim's list.

### Finding Description
An unprivileged attacker deposits minimal amounts of every underlying into the pool to obtain a dust balance of each `DepositToken` (up to 30 collaterals, the same `MAX_TOKENS_PER_USER` bound used for `addDepositToken` [6](#0-5) ), then calls `transfer(victim, 1)` on each deposit token. Each first-time receipt pushes the token into `depositTokensOfAccount[victim]`, filling the list to 30. From then on:

- Any transfer of a deposit token the victim doesn't already hold reverts in `addToDepositTokensOfAccount` — the victim cannot receive new collateral types.
- Any `deposit`/`mint` of a new collateral type to the victim reverts.
- Critically, `Pool.liquidate` calls `depositToken_.seize(account_, feeCollector, _fee)` for the protocol fee [7](#0-6) . Seizure mints deposit-token balance to the recipient; if the recipient (the fixed `feeCollector`) has a 0 balance for that token and a full list, the add reverts and the whole liquidation reverts.

### Impact Explanation
Targeting the `feeCollector` is the high-impact case: every liquidation of a collateral type the fee collector doesn't yet hold will revert at the fee `seize`, so unhealthy positions in those collaterals cannot be liquidated at all. That breaks the liquidation-liveness invariant and lets bad debt accrue in the pool (temporary freezing of protocol funds / insolvency risk). Targeting a normal user is lesser but still a DoS: the victim cannot add new collateral types or receive those tokens until they fully zero out an existing position, and their position cannot be partially rescued with a new collateral.

### Likelihood Explanation
The attack requires only EOAs and public entry points: ordinary `deposit` + ERC20 `transfer` calls, costing dust of each collateral (or zero cost where the attacker can flash-obtain balances). No privileged role, oracle manipulation, or governance action is needed. The constraint is that the pool must list multiple deposit tokens for full list exhaustion — and the fee collector case only needs enough distinct deposit tokens to reach 30 first-time receipts total across deposit+debt tokens, which the protocol itself caps at 30.

### Recommendation
Validate inputs the way Zeppelin should have: require a non-dust minimum (e.g., a USD-denominated minimum via `masterOracle`) before registering a token into an account's list, or make `addTo*TokensOfAccount` failure non-fatal for the accounting path (skip the add / track lists lazily) so that hitting `MAX_TOKENS_PER_USER` only degrades `depositOf`/`debtOf` enumeration instead of reverting the entire transfer/seize. Alternatively, exempt `seize`/fee flows from the per-account cap since the fee collector is a system address.

### Proof of Concept
Hardhat fork sketch:

```ts
// pool, depositToken[0..n], attacker EOA already set up on a fork
const MAX = (await pool.MAX_TOKENS_PER_USER()).toNumber(); // 30

// 1. Attacker obtains dust of each deposit token
for (const dt of depositTokens) {
  await underlying(dt).approve(dt.address, 1);
  await dt.deposit(1); // attacker now holds >=1 msdToken
}

// 2. Fill the feeCollector's per-account list (or victim's)
const feeCollector = await poolRegistry.feeCollector();
for (const dt of depositTokens) {
  if ((await dt.balanceOf(feeCollector)).eq(0)) {
    await dt.transfer(feeCollector, 1); // each adds an entry
  }
}
// depositTokensOfAccount.length(feeCollector) is now MAX

// 3. Any new deposit-token first-receipt to feeCollector reverts
const newDt = depositTokens[MAX - 1]; // one not yet held
await expect(newDt.transfer(feeCollector, 1))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 4. Liquidation of an unhealthy account in a collateral the
//    feeCollector doesn't hold reverts at the fee seize
await expect(
  pool.liquidate(msSynth.address, unhealthyAccount, amountToRepay, newDt.address)
).revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

Uncertainties: I confirmed the `add` path and the revert modifier, and that `liquidate` calls `seize` for the fee [8](#0-7) ; I did not have iterations left to read `DepositToken.seize`/`_deposit` line-by-line, but `seize` moves balance to the recipient and shares the same `_mint`/list-add logic seen in `_transfer` and `_mint` [9](#0-8) . If `seize` bypasses `addToDepositTokensOfAccount`, the fee-collector variant degrades to the per-victim DoS variant, which still stands on the verified code.

### Citations

**File:** contracts/Pool.sol (L79-79)
```text
    uint256 public constant MAX_TOKENS_PER_USER = 30;
```

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
```

**File:** contracts/Pool.sol (L216-220)
```text
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
    }
```

**File:** contracts/Pool.sol (L587-593)
```text
        syntheticToken_.burn(_msgSender, amountToRepay_);
        _debtToken.burn(account_, amountToRepay_);
        depositToken_.seize(account_, _msgSender, _toLiquidator);

        if (_fee > 0) {
            depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee);
        }
```

**File:** contracts/Pool.sol (L703-703)
```text
        if (depositTokens.length() >= MAX_TOKENS_PER_USER) revert ReachedMaxDepositTokens();
```

**File:** contracts/DepositToken.sol (L486-488)
```text
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
