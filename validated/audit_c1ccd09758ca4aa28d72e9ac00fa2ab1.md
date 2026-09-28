### Title
Attacker can saturate a victim's per-account token list with dust deposits, blocking them from adding new collateral/debt token types (DoS) - ([File: contracts/Pool.sol](contracts/Pool.sol), [File: contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
The MySQL CVE is an availability bug: a repeatable, cheap-to-trigger denial of service. The strongest reachable analog in Metronome is the `MAX_TOKENS_PER_USER` accounting limit combined with the fact that `DepositToken.deposit(amount_, onBehalfOf_)` lets anyone mint deposit-token balances to an arbitrary account without consent. An unprivileged attacker can dust-fill a victim's `depositTokensOfAccount` set until `debtTokensOfAccount + depositTokensOfAccount >= 30`, after which every path that adds a new token to the victim reverts with `UserReachedMaxTokens`.

### Finding Description
`DepositToken.deposit` pulls `underlying` from the caller and mints `msdTOKEN` to `onBehalfOf_`, with no opt-in from the beneficiary. `_mint` calls `pool.addToDepositTokensOfAccount(account_)` whenever the recipient's prior balance is 0 [1](#0-0) [2](#0-1) . The same forced-add happens on plain `transfer`/`transferFrom` of a deposit token to a first-time holder [3](#0-2) .

On the Pool side, both `addToDepositTokensOfAccount` and `addToDebtTokensOfAccount` are gated by `onlyIfAdditionWillNotReachMaxTokens`, which reverts once the combined per-account count reaches `MAX_TOKENS_PER_USER = 30` [4](#0-3) [5](#0-4) [6](#0-5) .

Because `addDepositToken` allows up to `MAX_TOKENS_PER_USER` deposit tokens per pool [7](#0-6) , an attacker who holds dust of each whitelisted collateral can:

```solidity
for (uint256 i; i < pool.getDepositTokens().length; ++i) {
    IDepositToken dt = IDepositToken(tokens[i]);
    dt.underlying().approve(address(dt), amount);
    dt.deposit(amount, victim); // adds dt to victim's depositTokensOfAccount
}
```

Once the victim's set reaches 30 entries:

- `DebtToken.issue` → `_mint` → `addToDebtTokensOfAccount` reverts, so the victim cannot open debt in any new synthetic asset [8](#0-7) .
- `DepositToken.deposit`/`transfer`/`transferFrom`/`seize` of any token the victim doesn't already hold reverts — the victim cannot be paid, cannot receive collateral top-ups of a new type, and cannot add a *new* collateral type to rescue a deteriorating position.
- `SmartFarmingManager.leverage`/`flashRepay` paths that mint new deposit or debt tokens to the user also revert through the same modifier.

No modifier blocks this: `deposit` is `whenNotPaused` only, accepts any `onBehalfOf_ != address(0)`, and the attacker needs no role — only real underlying dust (1 wei per collateral when `depositFee == 0`; slightly more otherwise so that `_deposited > 0`).

### Impact Explanation
Availability / liveness invariant: a user's ability to manage their position is gated by set size, which an attacker can grow on the user's behalf. The victim is temporarily unable to (a) issue any new debt token, (b) deposit or receive any new collateral type, and (c) be paid in tokens they don't already hold. During adverse price movement, a victim whose remaining issuable margin requires a *different* collateral type is unable to recapitalize and is exposed to liquidation — the attack converts a recoverable position into a forced liquidation window. The DoS is temporary (the victim frees slots by fully withdrawing the dust deposits, which the attacker paid for), but the attacker can front-run each `withdraw`/`removeFromDepositTokensOfAccount` with another dust deposit to keep the set saturated, at small but real cost per refill. This matches the CVE class: repeatable attacker-triggered denial of a core function, no privilege required.

### Likelihood Explanation
- Reachable from a single public entry point (`DepositToken.deposit`) with attacker-chosen `onBehalfOf_`; no privileged role, oracle manipulation, or flash loan needed.
- Cost scales with the number of whitelisted collateral types and their dust prices; on pools with many collaterals the cost is higher, but even on pools with few collaterals the same attack works via repeated dust `transfer`s of deposit tokens the attacker already holds.
- Mitigation exists: the victim can self-unblock by withdrawing each dust deposit to zero. This caps severity at Medium — temporary freezing of functionality rather than permanent loss — and requires an attacker willing to race the victim's cleanup transactions.

### Recommendation
- Require beneficiary consent for first-time additions: gate `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` so tokens can only be added when the account itself initiated the state-changing call (e.g., skip set-add on `deposit`/`transfer` to third parties, or make `deposit` only mint to `_msgSender()` / an explicitly registered beneficiary).
- Alternatively, replace the hard `UserReachedMaxTokens` revert with set-entry recycling: if the added entry has zero balance contribution for the account, auto-remove an existing dust entry instead of reverting.
- A minimal fix is to only call `addToDepositTokensOfAccount` inside `deposit` when `onBehalfOf_ == _msgSender()`, and to require an allowance-style opt-in (e.g., `permitReceive`) for first-time receipt via `transfer`.

### Proof of Concept
Hardhat (local, mirrors existing `test/Pool.test.ts` fixture style):

```ts
it('attacker saturates victim token list and blocks new collateral/debt', async () => {
    const {pool, depositTokens, victim, attacker, usdc, weth /* ...all underlyings */} = await loadFixture(fixture);

    // Victim starts with 1 collateral and 1 debt position
    await deposit(usdc, victim, parseUnits('1000', 6));
    await msUsdDebtToken.connect(victim).issue(victim.address, parseEther('500'));

    // Attacker dust-deposits every other whitelisted collateral to the victim
    for (const dt of depositTokens) {
        const underlying = await ethers.getContractAt('ERC20', await dt.underlying());
        if ((await dt.balanceOf(victim.address)).isZero()) {
            await underlying.connect(attacker).approve(dt.address, 10);
            await dt.connect(attacker).deposit(10, victim.address); // _deposited >= 1
        }
    }

    // Victim's combined set is now at MAX_TOKENS_PER_USER (30)
    const n = (await pool.getDepositTokensOfAccount(victim.address)).length
            + (await pool.getDebtTokensOfAccount(victim.address)).length;
    expect(n).to.eq(30);

    // Victim can no longer deposit a NEW collateral type (if any remain unregistered to them)
    // nor issue a NEW synthetic's debt token:
    await expect(
        msEthDebtToken.connect(victim).issue(victim.address, 1)
    ).to.revertedWithCustomError(pool, 'UserReachedMaxTokens');

    // And cannot receive a new deposit token via transfer:
    await expect(
        someNewDepositToken.connect(attacker).transfer(victim.address, 1)
    ).to.revertedWithCustomError(pool, 'UserReachedMaxTokens');
});
```

Note: this PoC assumes the pool's `getDepositTokens()` plus the victim's existing debt tokens can reach 30 entries; per `addDepositToken`, up to `MAX_TOKENS_PER_USER` deposit tokens may be registered, so saturation is achievable on pools configured near that bound. On smaller deployments the attacker would need the pool to actually have enough registered deposit/debt tokens — if a given deployment has far fewer than 30 registered tokens, the attack is not executable there, which is the main uncertainty in this finding.

### Citations

**File:** contracts/DepositToken.sol (L234-236)
```text
        _mint(onBehalfOf_, _deposited);

        emit CollateralDeposited(_msgSender, onBehalfOf_, amount_, _deposited, _fee);
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

**File:** contracts/Pool.sol (L703-705)
```text
        if (depositTokens.length() >= MAX_TOKENS_PER_USER) revert ReachedMaxDepositTokens();

        if (!depositTokens.add(depositToken_)) revert DepositTokenAlreadyExists();
```

**File:** contracts/DebtToken.sol (L262-268)
```text
        _mint(_pool, _masterOracle, _msgSender, amount_);

        (_issued, _fee) = quoteIssueOut(amount_);
        if (_fee > 0) {
            _syntheticToken.mint(_pool.feeCollector(), _fee);
        }
        _syntheticToken.mint(to_, _issued);
```
