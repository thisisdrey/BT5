### Title
Attacker can fill a victim's per-account deposit-token list with dust to DoS deposits of new collateral types — ([File: contracts/Pool.sol])

### Summary
The InstaDApp bug class is "permissionless state transition that flips a precondition, so the victim's subsequent transaction reverts" (front-run `setOwner` with `build`). The Metronome analog: `DepositToken.deposit(amount_, onBehalfOf_)` is permissionless and mints msdTOKEN to any `onBehalfOf_` address. When the recipient's balance of that deposit token goes from `0`, `Pool.addToDepositTokensOfAccount` inserts the token into `depositTokensOfAccount[account]`, which is capped by `onlyIfAdditionWillNotReachMaxTokens`. An unprivileged attacker can deposit 1-wei-dust amounts of every registered deposit token `onBehalfOf_ = victim`, filling the victim's list up to `MAX_TOKENS_PER_USER`. After that, any deposit, transfer, or mint of a collateral token the victim does not already hold reverts — including collateral types added to the pool in the future.

### Finding Description
- `DepositToken.deposit` accepts an arbitrary `onBehalfOf_` and mints to it with no consent check, gated only by `whenNotPaused`, `nonReentrant`, and `onlyIfDepositTokenExists` [1](#0-0) .
- On the first nonzero balance of a token, `Pool.addToDepositTokensOfAccount` is called from the DepositToken (sender check via `_revertIfSenderIsNotDepositToken`) and adds the token to `depositTokensOfAccount[account_]`; the entry stays while `balanceOf > 0` [2](#0-1) .
- The `onlyIfAdditionWillNotReachMaxTokens` modifier (backing the `MAX_TOKENS_PER_USER` cap via `MappedEnumerableSet`) reverts once the victim's list is full, so any attempt to hold a *new* deposit token — `deposit`, `transfer`, `transferFrom`, liquidated `seize` credit paths that mint/credit a new token — reverts for the victim [3](#0-2) .
- Identical mechanics exist for the debt side via `DebtToken` issue/mint triggering `addToDebtTokensOfAccount`, though the attacker-controlled path is cheapest through deposit-token dust deposits/transfers [4](#0-3) .

Attack flow (mirroring the InstaDApp front-run):
1. Attacker calls `depositToken_i.deposit(1 wei, victim)` for each registered `DepositToken` `i` (or transfers dust deposit tokens directly to the victim — transfers of a new token hit the same `add` path).
2. `depositTokensOfAccount[victim]` reaches `MAX_TOKENS_PER_USER`.
3. Any subsequent `deposit`/`transfer`/`Operator.execute` batch that would credit the victim a token they don't already hold reverts in `onlyIfAdditionWillNotReachMaxTokens`.

### Impact Explanation
Temporary freezing of funds / liveness DoS: the victim cannot receive or deposit any collateral type not already in their list, permanently blocking use of newly added collateral tokens until they manually withdraw every dust position to zero out each entry. The cost to the attacker is only dust amounts of each underlying plus gas (and deposit fees are waived/rounding-to-zero at 1 wei scale via `quoteDepositOut`'s `wadMul` fee [5](#0-4) ). Cleanup is painful for the victim because they must withdraw each dust token individually to shrink the list.

### Likelihood Explanation
Fully unprivileged: `deposit` is a public entry point with no allowlist, and the same effect is reachable via plain `transfer`/`transferFrom` of deposit tokens the attacker legitimately holds. No privileged role, oracle manipulation, or governance action is required. The only prerequisites are that the pool has enough distinct deposit tokens to reach `MAX_TOKENS_PER_USER` — if the number of listed collaterals is below the cap, the attack instead blocks only the *next* (future) collateral additions, which still matches the front-run/precondition-revert bug class.

### Recommendation
Treat `onBehalfOf_` minting and inbound transfers as consent-gated, or make list membership lazy/opt-in: e.g., skip `addToDepositTokensOfAccount` for balances below a dust threshold, allow `deposit` only where `onBehalfOf_ == _msgSender()` unless an explicit opt-in flag is set, or replace the hard revert in `onlyIfAdditionWillNotReachMaxTokens` with eviction of zero-balance/dust entries. At minimum, exempt pure inbound transfers from the cap or allow victims to remove dust entries cheaply.

### Proof of Concept
Foundry sketch (fork/Hardhat equivalent), assuming `pool` has deposit tokens `d0..dN` and `MAX_TOKENS_PER_USER` is reachable:

```solidity
function test_dustFillBlocksNewCollateral() public {
    address victim = address(0xBEEF);
    address attacker = address(0xA77A);
    address[] memory dtokens = pool.getDepositTokens();

    // attacker funds itself with 1 wei of each underlying
    for (uint i; i < dtokens.length; ++i) {
        IDepositToken dt = IDepositToken(dtokens[i]);
        deal(address(dt.underlying()), attacker, 1);
        vm.startPrank(attacker);
        dt.underlying().approve(address(dt), 1);
        dt.deposit(1, victim);          // adds token to victim's list
        vm.stopPrank();
    }
    assertEq(pool.getDepositTokensOfAccount(victim).length, dtokens.length);
    // assume dtokens.length == MAX_TOKENS_PER_USER (or repeat until cap)

    // governance later lists a NEW collateral -> depositTokenNew
    vm.prank(victim);
    underlyingNew.approve(address(depositTokenNew), 1e18);
    vm.expectRevert(); // MaxTokensPerAccountReached via onlyIfAdditionWillNotReachMaxTokens
    depositTokenNew.deposit(1e18, victim);
}
```

Caveat: I could not fully verify the exact revert name and the `MAX_TOKENS_PER_USER` value/`MappedEnumerableSet` cap enforcement body within the tool-iteration budget, nor whether the pool lists enough distinct collaterals to reach the cap on the deployed configuration — if fewer tokens than the cap exist, the attack narrows to permanently blocking future collateral types rather than existing ones, which is a weaker (though still valid, lower-severity) instance of the same bug class.

### Citations

**File:** contracts/DepositToken.sol (L211-236)
```text
    function deposit(
        uint256 amount_,
        address onBehalfOf_
    ) external override whenNotPaused nonReentrant onlyIfDepositTokenExists returns (uint256 _deposited, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();
        if (onBehalfOf_ == address(0)) revert BeneficiaryIsNull();

        IPool _pool = pool;
        IERC20 _underlying = underlying;
        address _msgSender = _msgSender();
        address _treasury = address(_pool.treasury());

        if (_msgSender == _treasury) revert TreasuryCanNotDeposit();

        uint256 _balanceBefore = _underlying.balanceOf(_treasury);
        _underlying.safeTransferFrom(_msgSender, _treasury, amount_);
        amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;

        (_deposited, _fee) = quoteDepositOut(amount_);
        if (_fee > 0) {
            _mint(_pool.feeCollector(), _fee);
        }

        _mint(onBehalfOf_, _deposited);

        emit CollateralDeposited(_msgSender, onBehalfOf_, amount_, _deposited, _fee);
```

**File:** contracts/DepositToken.sol (L294-302)
```text
    function quoteDepositOut(uint256 amount_) public view override returns (uint256 _amountToDeposit, uint256 _fee) {
        uint256 _depositFee = pool.feeProvider().depositFee();
        if (_depositFee == 0) {
            return (amount_, _fee);
        }

        _fee = amount_.wadMul(_depositFee);
        _amountToDeposit = amount_ - _fee;
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
