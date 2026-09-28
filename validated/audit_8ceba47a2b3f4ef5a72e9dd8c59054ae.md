### Title
Dust-transfer saturation of `depositTokensOfAccount` bricks fee collection, deposits, withdrawals and liquidations - ([File: contracts/Pool.sol])

### Summary
`Pool` caps the combined number of debt + deposit tokens tracked per account at `MAX_TOKENS_PER_USER = 30`. Every `DepositToken._transfer`/`_mint` that gives an account a token it does not already hold calls `Pool.addToDepositTokensOfAccount`, which reverts with `UserReachedMaxTokens` once the cap is hit. An unprivileged attacker can deposit 1 wei of every listed collateral and `transfer` dust to a target account — most importantly the protocol `feeCollector` — permanently saturating its token list. After that, any code path that mints or transfers a not-yet-held deposit token to the saturated account reverts, causing a repeatable, unconditional DoS of fee-charging operations.

### Finding Description
- `Pool.onlyIfAdditionWillNotReachMaxTokens` reverts when `debtTokensOfAccount.length + depositTokensOfAccount.length >= MAX_TOKENS_PER_USER` (30) [1](#0-0) 
- `addToDepositTokensOfAccount` is invoked from `DepositToken._mint` and `DepositToken._transfer` whenever the recipient's balance was zero [2](#0-1) [3](#0-2) 
- The pool itself may list up to 30 deposit tokens (`addDepositToken` allows `depositTokens.length() < MAX_TOKENS_PER_USER`), so an attacker can push any address to the cap with 30 dust transfers [4](#0-3) 
- Once the `feeCollector`'s list is saturated, every operation that pays fees in a deposit token it does not yet hold reverts:
  - `DepositToken._withdraw` → `_transfer(account_, feeCollector, _fee)` reverts → all fee-bearing `withdraw`/`flashWithdraw`/`withdrawFrom` revert [5](#0-4) 
  - `DepositToken.deposit` → `_mint(feeCollector, _fee)` reverts → all fee-bearing deposits revert [6](#0-5) 
  - `Pool.liquidate` → `depositToken_.seize(account_, feeCollector, _fee)` reverts → all liquidations on pools where `protocolFee > 0` revert, leaving unhealthy positions unclosable [7](#0-6) 

Note the `add` in `addToDepositTokensOfAccount` uses a set — `DepositTokenAlreadyExists` only fires on a re-add of a held token, and the balance>0 short-circuit prevents that — so there is no way to remove entries except the recipient spending each token down to exactly zero, which the feeCollector (a privileged/protocol address) realistically never does for dust of every collateral.

### Impact Explanation
Availability / solvency impact matching the bug class (repeatable crash → DoS). On any pool whose `depositTokens` set is (or becomes, as governance adds collaterals) close to 30 entries:
1. Saturating `feeCollector` makes **every** liquidation that charges a protocol fee revert inside `Pool.liquidate`, so underwater positions cannot be closed → protocol accrues bad debt (insolvency risk).
2. All deposits and withdrawals with non-zero fees revert → user funds are temporarily frozen (users can only exit via zero-fee paths or by waiting for governance intervention).
3. Saturating an arbitrary victim blocks them from receiving any new deposit-token type — including topping up collateral to avoid liquidation — enabling forced liquidation of the victim's position.

The attack requires only an EOA, public `deposit`/`transfer` calls, and ~30 dust transfers — no privileged role, oracle manipulation, or governance action.

### Likelihood Explanation
- Reachable whenever the pool lists enough deposit tokens that attacker dust plus the victim's existing tokens can reach 30; `addDepositToken` explicitly permits up to `MAX_TOKENS_PER_USER` collaterals [8](#0-7) .
- Debt tokens share the same 30-slot budget, so accounts holding issued synth debt reach the cap sooner.
- Cost is trivial (30 × 1-wei deposits + transfers). No checks (`SynthContext`, `nonReentrant`, pause flags, caps) prevent it: `transfer` only verifies the *sender's* unlocked balance [9](#0-8) .
- On sparsely populated pools (<30 collaterals, no debt tokens on target) the attack cannot reach the cap today, which lowers likelihood on current deployments but the protocol is designed to grow toward that cap.

### Recommendation
- Do not count toward the cap — or do not revert — for transfers/mints below a meaningful dust threshold, or exempt the `feeCollector` (it can hold unbounded fee tokens).
- Alternatively, increase `MAX_TOKENS_PER_USER`, decouple it from the pool's own `addDepositToken` cap, or let `removeFromDepositTokensOfAccount` be triggered permissionlessly for zero-balance dust entries.
- A pragmatic fix: in `addToDepositTokensOfAccount`, skip the cap check when `account_ == poolRegistry.feeCollector()` since fee flows must never revert.

### Proof of Concept
Hardhat sketch (fork or existing fixture with pool listing ≥30 deposit tokens, or governance adds tokens to reach the cap):

```ts
// attacker: EOA; target: feeCollector
const feeCollector = await poolRegistry.feeCollector();
const depositTokens: DepositToken[] = [...]; // all listed deposit tokens
for (const dt of depositTokens) {
  if (await dt.balanceOf(feeCollector) === 0) {
    await dt.underlying().approve(dt.address, 1);
    await dt.deposit(1, attacker.address);          // mint dust to self
    await dt.transfer(feeCollector, 1);             // fills target's token list
  }
}
expect(await pool.getDepositTokensOfAccount(feeCollector)).length +
       (await pool.getDebtTokensOfAccount(feeCollector)).length === 30;

// 1) Liquidation DoS (protocolFee > 0):
await expect(
  pool.connect(liquidator).liquidate(msEth.address, victim, amount, msdNewCollateral.address)
).revertedWithCustomError(pool, 'UserReachedMaxTokens'); // via seize → feeCollector

// 2) Deposit DoS (depositFee > 0, collateral not yet held by feeCollector):
await expect(msdX.deposit(amount, alice.address))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens'); // via _mint(feeCollector, fee)

// 3) Withdraw DoS (withdrawFee > 0):
await expect(msdX.withdraw(amount, alice.address))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens'); // via _transfer → feeCollector
```

Victim-griefing variant: replace `feeCollector` with any account; afterwards `msdNew.deposit(_, victim)` and any inbound transfer of a new deposit token revert, so the victim cannot add collateral to restore health before liquidation.

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

**File:** contracts/Pool.sol (L581-593)
```text
        (_totalSeized, _toLiquidator, _fee) = quoteLiquidateOut(syntheticToken_, amountToRepay_, depositToken_);

        if (_totalSeized > depositToken_.balanceOf(account_)) {
            revert AmountIsTooHigh();
        }

        syntheticToken_.burn(_msgSender, amountToRepay_);
        _debtToken.burn(account_, amountToRepay_);
        depositToken_.seize(account_, _msgSender, _toLiquidator);

        if (_fee > 0) {
            depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee);
        }
```

**File:** contracts/Pool.sol (L698-707)
```text
    function addDepositToken(address depositToken_) external onlyGovernor {
        if (depositToken_ == address(0)) revert AddressIsNull();
        IERC20 _underlying = IDepositToken(depositToken_).underlying();
        if (address(depositTokenOf[_underlying]) != address(0)) revert UnderlyingAssetInUse();
        // Note: Fee collector collects deposit tokens as fee
        if (depositTokens.length() >= MAX_TOKENS_PER_USER) revert ReachedMaxDepositTokens();

        if (!depositTokens.add(depositToken_)) revert DepositTokenAlreadyExists();

        depositTokenOf[_underlying] = IDepositToken(depositToken_);
```

**File:** contracts/DepositToken.sol (L229-234)
```text
        (_deposited, _fee) = quoteDepositOut(amount_);
        if (_fee > 0) {
            _mint(_pool.feeCollector(), _fee);
        }

        _mint(onBehalfOf_, _deposited);
```

**File:** contracts/DepositToken.sol (L348-354)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
    }
```

**File:** contracts/DepositToken.sol (L486-488)
```text
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```

**File:** contracts/DepositToken.sol (L545-551)
```text
        (_withdrawn, _fee) = quoteWithdrawOut(amount_);
        if (_fee > 0) {
            _transfer(account_, _pool.feeCollector(), _fee);
        }

        _burn(account_, _withdrawn);
        _pool.treasury().pull(to_, _withdrawn);
```
