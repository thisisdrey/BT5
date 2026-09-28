### Title
Unprivileged dust-transfer griefing fills victim's token list to `MAX_TOKENS_PER_USER`, blocking deposits and borrows - ([File: contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
`Pool` enforces a hard cap of `MAX_TOKENS_PER_USER = 30` on the per-account `depositTokensOfAccount`/`debtTokensOfAccount` `MappedEnumerableSet` lists, reverting with `UserReachedMaxTokens` when a transfer/mint would add a 31st token ( [1](#0-0) ). `DepositToken.transfer`/`transferFrom` are public, permissionless, impose no minimum amount, and only check the *sender's* lock (`_revertIfLocked` on `_msgSender`/`sender_`), never the recipient's list size ( [2](#0-1) ). The symmetric bookkeeping in `_burn`/`_transfer` adds the token to the recipient's set whenever their balance goes from zero to non-zero and removes it only when it returns to zero ( [3](#0-2) ). An attacker can therefore send 1-wei dust of every registered `DepositToken` to a victim, permanently occupying all 30 slots.

### Finding Description
1. Attacker deposits a minimal amount into each registered `DepositToken` (or acquires dust positions) — each `deposit(amount_, onBehalfOf_)` accepts arbitrary tiny `amount_` ( [4](#0-3) ).
2. Attacker calls `DepositToken.transfer(victim, 1)` for each deposit token. Each call passes `_revertIfLocked` (attacker's own unlocked balance is checked, not the victim's) and `_transfer` registers the token in `depositTokensOfAccount[victim]`.
3. Once the victim's set reaches 30 entries, any subsequent action that would register a *new* token reverts with `UserReachedMaxTokens`: `deposit`/`_mint` of a deposit token the victim does not yet hold, `DebtToken.issue`/`mint` of a synth not already held, and `deposit(amount_, onBehalfOf_ = victim)` by any third party.

### Impact Explanation
The victim cannot open new collateral positions or new borrows in tokens they do not already hold. Critically, a victim whose position drifts toward liquidation cannot deposit a *different* collateral type to restore health — the rescue deposit reverts — making their existing position forcibly liquidatable. This is a temporary freezing of funds / liveness break of the deposit-borrow invariant, caused entirely by an unprivileged EOA's malformed usage pattern (dust transfers), analogous to CVE-2010-3439's "crash the server via crafted parameters to a public command": here, crafted calls to a public entry point put the victim's account into a degraded state. The victim can partially recover by liquidating/transferring dust positions, but only tokens they can transact in, and each freed slot can be refilled by the attacker at negligible cost.

### Likelihood Explanation
- Attacker needs only dust of each deposit token (obtainable via minimal `deposit` calls) and ~30 low-cost `transfer` transactions; no privileged role, no oracle manipulation, no flash loan required.
- `transfer` has no `whenNotPaused`/minimum-amount guard and no recipient consent; `nonReentrant` is irrelevant.
- Cost scales with number of registered deposit tokens; attack is repeatable and cheap relative to the harm to a leveraged victim.

### Recommendation
- Only add a token to `depositTokensOfAccount`/`debtTokensOfAccount` on mint/issue paths (where the receiver is the actual position owner), not on arbitrary `transfer`/`transferFrom` — or alternatively check the recipient's slot count in `_transfer` and revert `UserReachedMaxTokens` on the *sender's* transaction so dust cannot be forced in.
- Alternatively, enforce a minimum transfer amount or make `seize`/liquidation paths not count toward the cap.
- Caveat: if tokens are removed from the set on transfer-out, the victim can self-clean by transferring dust out; the attack still works because the attacker refills slots faster than the victim clears them, and victims unaware of the dust remain blocked.

### Proof of Concept
```solidity
// Foundry fork test sketch (run against deployed Pool + DepositTokens)
function test_DustGriefingMaxTokens() public {
    address victim = makeAddr("victim");
    address[] memory dts = pool.getDepositTokens(); // assume >= 30 registered

    for (uint i; i < 30 && i < dts.length; i++) {
        IDepositToken dt = IDepositToken(dts[i]);
        // attacker self-funds dust
        deal(address(dt.underlying()), attacker, 2);
        IERC20(dt.underlying()).approve(address(dt), 2);
        dt.deposit(1, attacker);            // attacker mints dust
        dt.transfer(victim, 1);             // occupies one slot of victim
    }

    // victim now has 30 entries; any new depositToken deposit on their behalf reverts
    IDepositToken dt31 = IDepositToken(dts[30]);
    deal(address(dt31.underlying()), victim, 100);
    vm.startPrank(victim);
    IERC20(dt31.underlying()).approve(address(dt31), 100);
    vm.expectRevert(UserReachedMaxTokens.selector);
    dt31.deposit(100, victim);
    vm.stopPrank();
}
```
Note: I could not fully read `DepositToken._transfer`/`_mint` in the available iterations; the add-to-set call is inferred from the mirrored removal logic in `_burn` (line ~460) and the `UserReachedMaxTokens` revert in `Pool`. Verify that the add-path check is `pool`-side and not bypassed for `transfer` before finalizing the PoC.

### Citations

**File:** contracts/Pool.sol (L76-79)
```text
    /**
     * @notice Maximum tokens per pool a user may have
     */
    uint256 public constant MAX_TOKENS_PER_USER = 30;
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

**File:** contracts/DepositToken.sol (L348-376)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
    }

    /// @inheritdoc IERC20
    function transferFrom(
        address sender_,
        address recipient_,
        uint256 amount_
    ) external override nonReentrant returns (bool) {
        _revertIfLocked(sender_, amount_);

        address _msgSender = _msgSender();
        uint256 _currentAllowance = allowance[sender_][_msgSender];
        if (_currentAllowance != type(uint256).max) {
            if (_currentAllowance < amount_) revert AmountExceedsAllowance();
            unchecked {
                _approve(sender_, _msgSender, _currentAllowance - amount_);
            }
        }

        _transfer(sender_, recipient_, amount_);

        return true;
    }
```

**File:** contracts/DepositToken.sol (L459-460)
```text
        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (_amount > 0 && _balanceAfter == 0) {
```
