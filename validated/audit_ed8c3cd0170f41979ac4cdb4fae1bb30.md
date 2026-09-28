### Title
Attacker can permanently brick `Pool.liquidate` and `DepositToken.deposit` by filling the fee collector's `depositTokensOfAccount` list to `MAX_TOKENS_PER_USER` via dust transfers - (File: contracts/Pool.sol)

### Summary
`Pool` enforces a per-account cap of `MAX_TOKENS_PER_USER = 30` entries across `debtTokensOfAccount` + `depositTokensOfAccount` in `onlyIfAdditionWillNotReachMaxTokens` [1](#0-0) . `DepositToken.transfer` is permissionless for any unlocked balance, and `_transfer` calls `pool.addToDepositTokensOfAccount(recipient)` whenever the recipient's balance goes `0 -> >0` [2](#0-1) [3](#0-2) . The protocol fee collector is an ordinary account in this list: `deposit()` mints deposit fees to it via `_mint(_pool.feeCollector(), _fee)` [4](#0-3) , and `Pool.liquidate` seizes the protocol fee to it via `depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee)` [5](#0-4) . Once the collector's list reaches 30 entries, every code path that adds a *new* deposit token to it reverts with `UserReachedMaxTokens` — including liquidations and deposits on collateral types it does not yet hold.

### Finding Description
Bug class (analog of a remote packet-triggered DoS): an unprivileged attacker sends 1-wei dust `DepositToken.transfer(...)` calls to the fee collector address for every deposit token the collector doesn't already track. Because `_transfer` unconditionally registers the recipient on first receipt and the add path reverts at the cap, the attacker can deterministically push the collector to `MAX_TOKENS_PER_USER`.

After that, two victim-facing paths revert:

1. `Pool.liquidate`: `quoteLiquidateOut` computes `_fee > 0` whenever `feeProvider.liquidationFees()._protocolFee > 0` [6](#0-5) , then `seize(account_, feeCollector, _fee)` → `_transfer` → `addToDepositTokensOfAccount(feeCollector)` reverts whenever `depositToken_` is a collateral the collector doesn't already hold.
2. `DepositToken.deposit`: `_mint(feeCollector, _fee)` reverts identically whenever `depositFee > 0` and the deposit token isn't already in the collector's list.

Note the same attack applies to `swap`/interest-fee flows whenever they mint to the fee collector a token it doesn't yet hold. The collector (typically a Treasury/Governor-controlled contract) cannot cheaply free slots: removal requires its balance of a tracked token to hit zero [7](#0-6) , which needs a privileged sweep/transfer of each dust token — so the DoS persists until governance acts.

### Impact Explanation
While the collector's list is saturated, liquidations on any collateral type absent from the list revert unconditionally. Underwater positions cannot be liquidated, so bad debt accrues against the protocol (solvency invariant broken) and honest liquidators are blocked. Deposits bearing a deposit fee are likewise bricked for untracked collateral types — a temporary freeze of protocol functionality caused entirely by an unprivileged EOA, mirroring the CVE's remote, unauthenticated DoS.

### Likelihood Explanation
Cost is bounded: the attacker must acquire a dust balance of each missing deposit token (at most 30 deposits of 1 wei of each underlying, withdrawable afterward) and send ~30 public `transfer` calls. No privileged role, oracle manipulation, flash loan, or off-chain dependency is required; `transfer` only checks the *sender's* lock via `_revertIfLocked(_msgSender, amount_)` [8](#0-7) , so the attacker just needs a debt-free funding account. Success depends only on the deployed configuration having nonzero `depositFee`/`liquidationFees()._protocolFee`, which is the normal fee configuration.

### Recommendation
Do not gate `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` for privileged protocol sink addresses (e.g. skip the `MAX_TOKENS_PER_USER` check when `account_ == feeCollector` or make the check opt-in), or seize/mint liquidation and deposit fees to a dedicated contract exempt from per-account tracking. Alternatively, have `seize`/`_mint` to the fee collector bypass list registration entirely, since the collector never opens a debt position and the list only exists to compute `debtPositionOf`.

### Proof of Concept
```solidity
// Foundry fork test sketch (mainnet Pool + DepositTokens)
// Assumption: feeProvider.liquidationFees()._protocolFee > 0 (deployed config).

// 1. Attacker deposits 1 wei of each underlying into every DepositToken in the pool,
//    receiving dust msdTOKEN balances on an attacker-controlled, debt-free EOA.
for (uint i; i < allDepositTokens.length; ++i) {
    IERC20 underlying = allDepositTokens[i].underlying();
    deal(address(underlying), attacker, 1);
    underlying.approve(address(allDepositTokens[i]), 1);
    allDepositTokens[i].deposit(1, attacker);          // attacker balance > 0
}

// 2. Attacker dust-transfers every deposit token to the fee collector,
//    filling debtTokensOfAccount + depositTokensOfAccount[feeCollector] to 30.
address feeCollector = poolRegistry.feeCollector();
for (uint i; i < allDepositTokens.length && i < 30; ++i) {
    allDepositTokens[i].transfer(feeCollector, 1);     // adds token to collector's list
}
assertEq(
    pool.getDepositTokensOfAccount(feeCollector).length
      + pool.getDebtTokensOfAccount(feeCollector).length,
    pool.MAX_TOKENS_PER_USER()
);

// 3. Victim position goes unhealthy (price move). Any liquidation whose seized
//    collateral token is NOT already in the collector's list reverts.
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
pool.liquidate(msETH, victim, amountToRepay, newCollateralDepositToken);

// 4. Likewise, a user deposit (depositFee > 0) into an untracked token reverts
//    inside _mint(feeCollector, _fee).
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
newCollateralDepositToken.deposit(amount, bob);
```

Reproduction notes: use a mainnet/Optimism fork at the deployed `Pool` (`VERSION = "1.3.2"`), pick a `depositToken_` absent from `getDepositTokensOfAccount(feeCollector)` after step 2 for the `liquidate`/`deposit` calls, and confirm `feeProvider.depositFee()`/`liquidationFees()._protocolFee` are nonzero so the fee mint/seize path is exercised.

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

**File:** contracts/Pool.sol (L216-220)
```text
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
    }
```

**File:** contracts/Pool.sol (L462-471)
```text
        (uint128 _liquidatorIncentive, uint128 _protocolFee) = feeProvider.liquidationFees();

        if (_protocolFee > 0) {
            _fee = _toLiquidator.wadMul(_protocolFee);
        }
        if (_liquidatorIncentive > 0) {
            _toLiquidator += _toLiquidator.wadMul(_liquidatorIncentive);
        }

        _totalToSeize = _fee + _toLiquidator;
```

**File:** contracts/Pool.sol (L591-593)
```text
        if (_fee > 0) {
            depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee);
        }
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

**File:** contracts/DepositToken.sol (L459-462)
```text
        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (_amount > 0 && _balanceAfter == 0) {
            pool.removeFromDepositTokensOfAccount(_account);
        }
```
