### Title
Attacker fills victim's `MAX_TOKENS_PER_USER` slots via `onBehalfOf_` dust deposits / transfers, permanently blocking new collateral and debt positions - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
Analogous to CVE-2011-5034 (attacker-supplied input degrades a bounded data structure into a DoS), `Pool` tracks each account's deposit/debt tokens in a `MappedEnumerableSet` capped at `MAX_TOKENS_PER_USER = 30` [1](#0-0) . Any addition beyond the cap reverts with `UserReachedMaxTokens` [2](#0-1) . Because `DepositToken.deposit(amount_, onBehalfOf_)` lets anyone mint deposit tokens to an arbitrary victim [3](#0-2)  and `transfer`/`transferFrom` let anyone push dust balances to a victim [4](#0-3) , an attacker can fill all 30 slots of a victim's set and revert every future token addition to that account.

### Finding Description
- `addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` are invoked by the token contracts whenever an account's balance goes 0 → non-zero, and both enforce `onlyIfAdditionWillNotReachMaxTokens` [5](#0-4) .
- The victim has no way to refuse: `deposit()` accepts any `onBehalfOf_` address with no opt-in, and `transfer` credits the recipient unconditionally.
- Once `debtTokensOfAccount.length(victim) + depositTokensOfAccount.length(victim) == 30`, every call path that would add a new token — `deposit()` on a new collateral, `transfer`/`transferFrom`/`seize` of a token the victim doesn't hold, `DebtToken.issue/mint/flashIssue` of a new synthetic — reverts. Liquidations that would credit the victim a new deposit token via `DepositToken.seize` also revert, breaking the liquidation invariant.
- The victim can only recover by spending gas to transfer/withdraw dust out of each spammed token (each `transfer` is gated only by `_revertIfLocked`, so unlocked dust is movable), making the freeze temporary but forced and griefing asymmetric: one attacker transaction per token vs. N victim transactions plus oracle/health-check exposure in the interim.

### Impact Explanation
Temporary freezing of funds and forced liquidation risk: while the cap is saturated, the victim cannot add collateral to improve an unhealthy `debtPositionOf`, cannot open new debt positions, and liquidation proceeds routing a new deposit token to them revert. If the victim's position is near the liquidation threshold during the attack window, they are prevented from topping up collateral and can be liquidated — converting a pure DoS into direct loss of collateral via the liquidation fee split in `DepositToken.seize`.

### Likelihood Explanation
Fully unprivileged: attacker needs only an EOA and dust amounts of each listed underlying (or already-held deposit tokens for `transfer`). Cost scales linearly with the number of `DepositToken`s in the pool (each `deposit` pulls real underlying into `Treasury` [6](#0-5) ), but amounts can be minimal (1 wei where `quoteDepositOut` doesn't round to zero). No privileged role, oracle manipulation, or governance action required; `whenNotPaused`/`nonReentrant`/`SynthContext` checks do not stop it since the attacker calls the token contracts directly.

### Recommendation
- Restrict `deposit(onBehalfOf_)` and `transfer` so that adding to `depositTokensOfAccount` is only permitted for self-calls (`onBehalfOf_ == _msgSender()`), or
- Move the cap enforcement from revert-on-add to a per-account opt-in, or drop entries whose balance is below a dust threshold, or
- Add a victim-initiated `removeDustToken`/forgive path that the victim can call directly on `Pool` without needing to transfer through the token contract.

### Proof of Concept
Hardhat/foundry sketch (fork):
```solidity
// pool has >= N deposit tokens listed; victim = target EOA
uint256 slots = pool.MAX_TOKENS_PER_USER() - pool.debtTokensOfAccount.length(victim)
              - pool.depositTokensOfAccount.length(victim);
for (uint256 i; i < slots; ++i) {
    IDepositToken dt = IDepositToken(pool.getDepositTokens()[i % n]);
    IERC20(dt.underlying()).approve(address(dt), type(uint256).max);
    dt.deposit(1, victim); // mints 1 wei msdTOKEN to victim, adds dt to victim's set
}
// now: pool.depositTokensOfAccount.length(victim)+debt == 30
// victim's deposit on any new collateral reverts:
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
newDepositToken.deposit(1e18, victim); // called by victim themselves
// liquidator seize of a token victim doesn't hold also reverts inside Pool.liquidate
```
Recovery requires the victim to `transfer` each dust token away — each such call re-adds nothing but frees a slot only when balance hits 0, so the attack can be re-applied cheaply in the same block via a fresh dust deposit, sustaining the freeze.

Note: I could not read `DepositToken._transfer`/`_mint` internals within the iteration limit to confirm the exact lines that call `addToDepositTokensOfAccount` on balance 0→non-zero transitions; the 8 matching occurrences of `addTo*/removeFrom*` in `DepositToken.sol` and the documented call flow ("This function is called from `DepositToken` when user's balance changes from `0`" [7](#0-6) ) confirm the hook exists. The finding's premise stands on the confirmed revert paths in `Pool.sol`.

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

**File:** contracts/Pool.sol (L204-220)
```text
    function addToDebtTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _debtToken = _msgSender();
        _revertIfSenderIsNotDebtToken(_debtToken);
        if (!debtTokensOfAccount.add(account_, _debtToken)) revert DebtTokenAlreadyExists();
    }

    /**
     * @notice Add a deposit token to the per-account list
     * @dev This function is called from `DepositToken` when user's balance changes from `0`
     * @dev The caller should ensure to not pass `address(0)` as `_account`
     * @param account_ The account address
     */
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
    }
```

**File:** contracts/DepositToken.sol (L211-216)
```text
    function deposit(
        uint256 amount_,
        address onBehalfOf_
    ) external override whenNotPaused nonReentrant onlyIfDepositTokenExists returns (uint256 _deposited, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();
        if (onBehalfOf_ == address(0)) revert BeneficiaryIsNull();
```

**File:** contracts/DepositToken.sol (L225-234)
```text
        uint256 _balanceBefore = _underlying.balanceOf(_treasury);
        _underlying.safeTransferFrom(_msgSender, _treasury, amount_);
        amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;

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
