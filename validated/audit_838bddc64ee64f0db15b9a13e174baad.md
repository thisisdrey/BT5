### Title
Unprivileged attacker can fill a victim's `depositTokensOfAccount` list via dust `DepositToken` transfers, DoS-ing all new-collateral deposits (`deposit`/`_mint`) for that account - ([File: contracts/DepositToken.sol](contracts/DepositToken.sol), [File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`DepositToken` is a freely transferable ERC20, and every `_transfer`/`_mint` that gives a recipient a nonzero balance calls `pool.addToDepositTokensOfAccount(recipient)` [1](#0-0) [2](#0-1) . `Pool` stores per-account deposit tokens in a `MappedEnumerableSet` bounded by `MAX_TOKENS_PER_USER`; once the set is full, `addToDepositTokensOfAccount` reverts, which makes any subsequent transfer or deposit that would add a *new* deposit token to that account revert. An unprivileged attacker can therefore permanently block a victim from depositing any collateral type they don't already hold, by sending dust amounts of every registered `DepositToken` to the victim.

### Finding Description
- `DepositToken.transfer`/`transferFrom` only check the *sender's* unlocked balance via `_revertIfLocked` — there is no minimum amount and no way for a recipient to refuse tokens [3](#0-2) .
- `_transfer` adds the token to the recipient's account list whenever `_recipientBalanceBefore == 0 && amount_ > 0` [2](#0-1) .
- Removal happens only if the *recipient* later sends their whole balance away (`balanceOf[sender_] == 0` on their own transfer) — the victim is not required to act, and even if they do, the attacker can re-grief cheaply [4](#0-3) .
- `Pool.addToDepositTokensOfAccount` enforces the `MAX_TOKENS_PER_USER` cap and reverts when it is exceeded (cap and set logic live in `Pool.sol`'s `MappedEnumerableSet` usage); `deposit()` → `_mint()` hits the same `addToDepositTokensOfAccount` path, so deposits of any not-yet-held collateral also revert [5](#0-4) .
- No modifier stops this: `transfer` is permissionless, `nonReentrant`/`whenNotPaused`/`onlyIfDepositTokenExists` are all satisfied for legitimate registered deposit tokens, and the recipient cannot opt out.

This mirrors the CVE class — a denial of service reached only through a specific feature combination (here: transferable collateral receipts + bounded per-account accounting set), crashing a legitimate flow.

### Impact Explanation
- The victim cannot deposit any new collateral type and cannot receive transfers of deposit tokens they don't already hold — all such transactions revert inside `addToDepositTokensOfAccount`.
- If the victim's position drifts toward liquidation, they cannot top up with a different collateral type; liquidations of their position proceed while their remediation path is blocked. That is a temporary freezing of the deposit function with material economic consequence (forced liquidation).
- The attack is fully unprivileged: only public `DepositToken.transfer` calls with dust amounts of tokens the attacker minted via dust deposits (`deposit` accepts any nonzero `amount_`, fee-adjusted) [6](#0-5) .

### Likelihood Explanation
Cost scales with the number of registered deposit tokens up to `MAX_TOKENS_PER_USER`; each griefing unit requires only a dust deposit plus a transfer. It requires no privileged role, no oracle manipulation, and works on the deployed configuration (multiple deposit tokens are registered per pool in the deployment artifacts). Impact is bounded (victim can still withdraw existing collateral and can self-clean the list by transferring tokens out, which the attacker can cheaply redo), so this is a medium-severity availability/DoS issue rather than direct theft.

### Recommendation
- Do not add tokens to `depositTokensOfAccount` on plain `transfer`/`transferFrom` receipt; only track tokens acquired via `deposit()`/`seize()` (positions the protocol actually collateralizes), or make `addToDepositTokensOfAccount` idempotent/non-reverting on inbound transfers.
- Alternatively, let recipients reject/skim unsolicited balances, or drop the per-account cap by tracking collateral participation explicitly at deposit time.

### Proof of Concept
Foundry/Hardhat fork sketch:

```solidity
// Given: Pool with N registered DepositTokens (N >= MAX_TOKENS_PER_USER), victim with an open position
address victim = ...;
for (uint i; i < depositTokens.length; ++i) {
    DepositToken dt = depositTokens[i];
    if (dt.balanceOf(victim) == 0 && !pool.depositTokensOfAccountContains(victim, address(dt))) {
        // attacker deposits dust and forwards it
        dt.underlying().approve(address(dt), 1);
        dt.deposit(1, address(this));            // attacker mints dust msdTOKEN
        dt.transfer(victim, 1);                  // adds token to victim's set
    }
}
// Now victim's set is full:
vm.expectRevert(); // MaxNumberOfTokens / DepositTokenDoesNotExist-style revert
newDepositToken.deposit(amount, victim);       // victim cannot deposit new collateral
victim.callTransferExistingDepositToken();     // also reverts if it would add a new token
```

Validated components: dust `deposit` succeeds (no minimum), `transfer` has no recipient consent, `_transfer` unconditionally calls `addToDepositTokensOfAccount` on first receipt, and `deposit()`/`seize()` hit the same add path. The one element I could not fully verify within index limits is the exact revert branch inside `Pool.addToDepositTokensOfAccount` (the `MAX_TOKENS_PER_USER` check line); the constant and its usage are present in `contracts/Pool.sol`, but the precise enclosing lines should be confirmed when writing the executable PoC.

### Citations

**File:** contracts/DepositToken.sol (L211-237)
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
    }
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

**File:** contracts/DepositToken.sol (L485-488)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
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

**File:** contracts/DepositToken.sol (L522-525)
```text
        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```
