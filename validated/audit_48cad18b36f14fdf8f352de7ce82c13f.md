### Title
DepositToken.approve overwrites allowance, enabling spender front-run to claim old + new allowance — (`contracts/DepositToken.sol`)

### Summary
`DepositToken.approve()` sets `allowance[owner][spender]` to an absolute value without requiring the prior allowance to be zero or fully consumed. If a token owner changes an existing non-zero allowance from N to M, the spender can front-run the second `approve` transaction with a `transferFrom` of N, then spend another M after the new allowance lands — extracting N+M tokens the owner never intended to authorize. The same pattern exists in `SyntheticToken.approve()`.

### Finding Description
`DepositToken` implements its own ERC20 logic (it does not inherit the OpenZeppelin `ERC20` dependency). Its `approve` directly writes the new allowance via `_approve`:

`contracts/DepositToken.sol:187-190`
```solidity
function approve(address spender_, uint256 amount_) external override returns (bool) {
    _approve(_msgSender(), spender_, amount_);
    return true;
}
```

`transferFrom` consumes the allowance in `contracts/DepositToken.sol:357-376`:
```solidity
uint256 _currentAllowance = allowance[sender_][_msgSender];
if (_currentAllowance != type(uint256).max) {
    if (_currentAllowance < amount_) revert AmountExceedsAllowance();
    unchecked {
        _approve(sender_, _msgSender, _currentAllowance - amount_);
    }
}
_transfer(sender_, recipient_, amount_);
```

Attack scenario:
1. Alice holds msdTOKEN (DepositToken shares backed by collateral in `Treasury`) and calls `approve(bob, N)`.
2. Alice later submits `approve(bob, M)` to change the allowance.
3. Bob sees the pending tx in the mempool and front-runs it with `transferFrom(alice, bob, N)`, receiving N unlocked shares (only bounded by `_revertIfLocked`, i.e. Alice's collateral-locked portion, not by anything related to the approval race).
4. Alice's `approve` lands, restoring Bob's allowance to M.
5. Bob calls `transferFrom(alice, bob, M)`, ending with N+M shares instead of the intended M.

Bob can then `withdraw` those unlocked msdTOKEN shares for underlying collateral held by the `Treasury` (`DepositToken.sol:406-412`), converting the stolen allowance into real assets.

`increaseAllowance`/`decreaseAllowance` exist as documented mitigations (`DepositToken.sol:195-203`), but `approve` remains exposed and is the standard entry point wallets and UIs call. The identical issue applies to `SyntheticToken.approve`/`transferFrom` (same ERC20 implementation pattern).

### Impact Explanation
Direct theft of user funds: the spender obtains up to N+M msdTOKEN (or msXYZ synthetic tokens) when the owner only consented to M, and msdTOKEN is redeemable for underlying collateral via `withdraw`, so the loss is realized in the underlying asset. Bounded by the victim's unlocked balance and by the allowance values involved.

### Likelihood Explanation
- Fully reachable by an unprivileged spender EOA via public `transferFrom`; no privileged role needed.
- Requires the victim to change a non-zero allowance to another non-zero value using `approve` (rather than `decreaseAllowance`/reset-to-zero), plus successful front-running. On chains with public mempools (e.g. mainnet, BSC) this is a standard, well-documented race; it is the classic ERC20 approve/transferFrom front-run.
- Impact per instance is bounded by the allowance amounts, so severity is medium at best; this is also a widely known ERC20 design caveat, which lowers novelty but does not eliminate the loss scenario.

### Recommendation
- Either revert in `approve` when changing from a non-zero allowance to another non-zero value (`require(amount_ == 0 || allowance[_msgSender()][spender_] == 0)`), or remove the absolute `approve` in favor of `increaseAllowance`/`decreaseAllowance` only.
- Alternatively document prominently that integrators must reset to 0 before setting a new allowance, as OpenZeppelin's `SafeERC20.safeApprove` enforces.

### Proof of Concept
Hardhat-style reproduction against the deployed `DepositToken` logic (existing test harness at `test/DepositToken.test.ts` already wires `metDepositToken` with a mocked pool):

```ts
// alice has deposited and holds `depositedAmount` msdTOKEN, bob is attacker
const N = parseUnits('100', 18)
const M = parseUnits('40', 18)

await metDepositToken.connect(alice).approve(bob.address, N)

// Bob front-runs Alice's second approve in the same "block":
// (use evm_setAutomine(false) or a foundry vm.prank ordering to interleave)
await metDepositToken.connect(bob).transferFrom(alice.address, bob.address, N)
await metDepositToken.connect(alice).approve(bob.address, M)
await metDepositToken.connect(bob).transferFrom(alice.address, bob.address, M)

expect(await metDepositToken.balanceOf(bob.address)).to.eq(N.add(M)) // N+M, not M
```

Foundry fork variant: prank alice → `approve(bob, N)`; prank bob → `transferFrom(alice, bob, N)`; prank alice → `approve(bob, M)`; prank bob → `transferFrom(alice, bob, M)`; then bob calls `withdraw(N+M, bob)` to realize underlying collateral from `Treasury`, subject to `unlockedBalanceOf(alice)` covering the amount. [1](#0-0) [2](#0-1)

### Citations

**File:** contracts/DepositToken.sol (L187-203)
```text
    function approve(address spender_, uint256 amount_) external override returns (bool) {
        _approve(_msgSender(), spender_, amount_);
        return true;
    }

    /**
     * @notice Atomically decrease the allowance granted to `spender` by the caller
     */
    function decreaseAllowance(address spender_, uint256 subtractedValue_) external returns (bool) {
        address _msgSender = _msgSender();
        uint256 _currentAllowance = allowance[_msgSender][spender_];
        if (_currentAllowance < subtractedValue_) revert DecreasedAllowanceBelowZero();
        unchecked {
            _approve(_msgSender, spender_, _currentAllowance - subtractedValue_);
        }
        return true;
    }
```

**File:** contracts/DepositToken.sol (L357-376)
```text
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
