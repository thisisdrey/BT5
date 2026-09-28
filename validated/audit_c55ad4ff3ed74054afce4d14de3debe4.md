### Title
Dust-transfer stuffing of `feeCollector`'s per-account token list permanently reverts fee mints/seizes, DoSing `deposit`, `withdraw`, and `liquidate` for new collateral types - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.addToDepositTokensOfAccount` enforces `MAX_TOKENS_PER_USER = 30` on the combined debt-token + deposit-token list of any account. An unprivileged attacker can send dust `DepositToken` transfers to `feeCollector`, filling its list. After that, every path that mints or seizes a *new* deposit-token type to `feeCollector` reverts with `UserReachedMaxTokens`, bricking deposits, fee-bearing withdrawals, and liquidations for those collaterals — mirroring the availability-only DoS class of CVE-2017-3459.

### Finding Description
`DepositToken._transfer` and `DepositToken._mint` call `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's prior balance is zero and the moved amount is nonzero. [1](#0-0) [2](#0-1) 

`Pool.addToDepositTokensOfAccount` applies `onlyIfAdditionWillNotReachMaxTokens`, which reverts when `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER` (30). [3](#0-2) [4](#0-3) 

Attacker reachability is trivial: `DepositToken.transfer(to_, amount_)` is public and only checks that the *sender's* balance is unlocked (`_revertIfLocked(_msgSender, amount_)`); an attacker with no debt has a fully unlocked balance, so transferring 1 wei of each deposit token to `feeCollector` succeeds and adds each token to `feeCollector`'s list. [5](#0-4) 

Once `feeCollector` reaches 30 entries:

- `deposit()` reverts in `_mint(_pool.feeCollector(), _fee)` for any deposit token not already in `feeCollector`'s list. [6](#0-5) 
- `_withdraw()` (used by `withdraw`, `withdrawFrom`, `flashWithdraw`) reverts on its fee mint the same way, freezing user collateral behind fee-bearing withdrawals.
- `liquidate()` reverts on `depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee)`, blocking liquidations whenever the seized collateral type isn't already in `feeCollector`'s list. [7](#0-6) 

Because the revert happens inside the token-transfer hook, no modifier, pause flag, reentrancy guard, or health check prevents it — the calls simply revert. `feeCollector` is a protocol contract; its list entries can only be removed if it transfers each token out (there is no permissionless removal path, and `TokenHolder` sweeping is `onlyGovernor` [8](#0-7) ), so the freeze persists until privileged intervention.

### Impact Explanation
Permanent-until-governor-action denial of service: deposits of affected collateral types revert, withdrawals with a nonzero `withdrawFee` revert, and `liquidate` reverts for affected collateral — unhealthy positions cannot be liquidated, degrading toward bad debt while collateral exits are frozen. This matches the external report's pure-availability (complete DoS) bug class, on a reachable, unprivileged path.

### Likelihood Explanation
Cost is dust transfers of deposit tokens the attacker can buy on-market (no governance, keeper, or oracle manipulation needed). Likelihood is gated by configuration: the pool must have enough listed deposit tokens (plus debt tokens already accrued by `feeCollector` from interest fees, which also count toward the 30-slot cap) for the attacker to reach `MAX_TOKENS_PER_USER`. Where the combined count can't reach 30, the same attack still reverts any operation adding a *new* token once the cap is hit. The attack is also repeatable after each governor cleanup.

### Recommendation
Don't apply `MAX_TOKENS_PER_USER` to protocol-owned sink addresses (skip the cap/add for `feeCollector`), or make `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` a no-op instead of reverting when the cap is hit for system accounts. Alternatively, let `feeCollector` pull accrued fees lazily rather than receiving per-token mints that grow its enumerable set.

### Proof of Concept
Hardhat fork outline:

```ts
// For each listed deposit token dt_i in pool.getDepositTokens():
//   1. attacker acquires dust of underlying_i, calls dt_i.deposit(1, attacker)
//   2. attacker calls dt_i.transfer(feeCollector, 1)
// Repeat until pool.depositTokensOfAccount.length(feeCollector)
//        + pool.debtTokensOfAccount.length(feeCollector) == 30
//
// Pick a deposit token X whose balanceOf(feeCollector) == 0
// (i.e., not yet in feeCollector's list — use a freshly added collateral):
//
// await expect(X.deposit(amount, alice)).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
// await expect(X.withdraw(unlocked, alice)).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens') // when withdrawFee > 0
// await expect(pool.liquidate(msX, victim, repayAmt, X)).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens') // when protocolFee > 0
```

The revert propagates from `addToDepositTokensOfAccount` inside `_mint`/`_transfer` (`seize`), confirming deposits, withdrawals, and liquidations all halt.

### Citations

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

**File:** contracts/DepositToken.sol (L485-488)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L492-493)
```text
    // solhint-disable-next-line no-empty-blocks
    function _requireCanSweep() internal view override onlyGovernor {}
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

**File:** contracts/Pool.sol (L591-593)
```text
        if (_fee > 0) {
            depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee);
        }
```
