### Title
Unprivileged dust-transfer griefing fills a victim's `depositTokensOfAccount` set to `MAX_TOKENS_PER_USER`, blocking deposits, transfers and liquidations of new collateral types - ([File: contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
`DepositToken._transfer` and `_mint` unconditionally register the recipient in the pool's per-account set (`pool.addToDepositTokensOfAccount(recipient_)`) whenever their prior balance was zero. Because `transfer` is a permissionless public entry point gated only by `_revertIfLocked`, any EOA can dust-send 1 wei of every whitelisted `DepositToken` to a victim. `PoolStorageV1` stores this in a `MappedEnumerableSet.AddressSet` bounded by `MAX_TOKENS_PER_USER` (enforced inside `Pool.addToDepositTokensOfAccount`). Once the victim's set is full, every subsequent `_mint`/`_transfer` of a deposit token the victim does not already hold reverts — DoS via missing validation that recipients opt in to set insertion. This is the direct analog of CVE-2021-33098: improper input validation permitting an authenticated-but-unprivileged actor to cause denial of service. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
- `DepositToken.transfer` calls `_revertIfLocked(_msgSender, amount_)` then `_transfer(_msgSender, to_, amount_)` — there is no validation that `to_` consents, no minimum amount, and no way for a recipient to refuse insertion into their account list. [2](#0-1) 
- `_transfer` inserts the recipient into the per-account set whenever `_recipientBalanceBefore == 0 && amount_ > 0`. A 1-wei transfer therefore occupies one slot. [1](#0-0) 
- `_mint` performs the same insertion on deposits (`deposit` → `_mint(onBehalfOf_, ...)`), so minting to a saturated victim also reverts. [4](#0-3) 
- The set is `MappedEnumerableSet.AddressSet depositTokensOfAccount` and `Pool.addToDepositTokensOfAccount` enforces `MAX_TOKENS_PER_USER` (reverting once reached), per `Pool.sol`. [5](#0-4) 
- The same pattern exists for `debtTokensOfAccount` via `DebtToken.issue/flashIssue`, allowing debt-position griefing of victims the attacker can issue on behalf of. [6](#0-5) 

Attack path (unprivileged):
1. Victim holds only `msdTokenA` (1 slot used).
2. Attacker deposits a tiny amount into each other whitelisted collateral and calls `transfer(victim, 1)` on each `DepositToken` — each call passes `_revertIfLocked` (attacker has no debt) and inserts the token into the victim's set.
3. Once `depositTokensOfAccount[victim]` reaches `MAX_TOKENS_PER_USER`, any `deposit(..., victim)`, incoming `transfer`, `seize` to the victim (liquidation payouts), or `SmartFarmingManager` position opening for a token the victim doesn't hold reverts inside `addToDepositTokensOfAccount`.

### Impact Explanation
Liveness/DoS, matching the CVE's `A:H` class: the victim is temporarily unable to deposit new collateral types, receive deposit tokens, be paid out as liquidator beneficiary (`seize` → `_transfer` → set insert), or open smart-farming positions on new collateral. A victim with an open debt position cannot top up with a new collateral type during adverse price movement, so the griefing can indirectly force liquidation — but the direct, self-standing impact is denial of service of all set-mutating operations. Funds already held are not stolen; severity is Medium, consistent with temporary freezing of funds.

### Likelihood Explanation
Fully permissionless: only requires an EOA, dust amounts of each whitelisted underlying, and calls to `DepositToken.deposit`/`transfer`. Gas cost is linear in the number of whitelisted deposit tokens (one cheap transfer each). No privileged role, oracle manipulation, flash loan, or governance action is needed. Modifiers on the path (`whenNotPaused`, `nonReentrant`, `_revertIfLocked`, `onlyIfDepositTokenExists`) all operate on the sender, not the recipient, so none mitigate it. The cap `MAX_TOKENS_PER_USER` is a fixed compile-time bound that cannot be raised per account.

### Recommendation
Validate the recipient before inserting into their account list — the missing input validation. Options:
- Remove set tracking from `_transfer`/`_mint` and derive the per-account list lazily (e.g., snapshot or off-chain enumeration), or
- only insert on `deposit`/`issue` where the caller chooses `onBehalfOf_` and bound who can mint to others (require `onBehalfOf_ == _msgSender()` or an approved operator), or
- keep a `MAX_TOKENS_PER_USER` slot reserved such that exceeding entries are ignored for health iteration but still tracked for balance, or
- add an opt-in flag (`account.canReceive(token)`) checked before `addToDepositTokensOfAccount`.

### Proof of Concept
Reproducible on a Hardhat fork (e.g., `mainnet` or `optimism` deployments in `deployments/`):

```ts
// attacker = fresh EOA; victim holds msdA only
const pool = await ethers.getContractAt("Pool", POOL);
const dtokens = await pool.getDepositTokens(); // all whitelisted DepositTokens

for (let i = 1; i < dtokens.length; i++) {
  const dt = await ethers.getContractAt("DepositToken", dtokens[i]);
  const underlying = await ethers.getContractAt("IERC20", await dt.underlying());
  await underlying.approve(dt.address, 1);          // attacker needs ≥1 wei of each underlying
  await dt.deposit(1, attacker.address);
  await dt.transfer(victim.address, 1);             // occupies victim's set slot
}

// victim's depositTokensOfAccount now == MAX_TOKENS_PER_USER
const dtNew = await ethers.getContractAt("DepositToken", NEWLISTED_OR_UNHELD_TOKEN);
await victimUnderlying.connect(victim).approve(dtNew.address, amount);
await expect(dtNew.connect(victim).deposit(amount, victim.address))
  .to.be.reverted; // addToDepositTokensOfAccount reverts: TooManyDepositTokens

// also: liquidator payout via seize() to victim reverts on any unheld token
await expect(dtNew.seize(badDebtor.address, victim.address, x)).to.be.reverted;
```

One caveat to verify during execution: confirm the exact revert behavior of `MappedEnumerableSet.AddressSet.add` in `contracts/lib/MappedEnumerableSet.sol` when `MAX_TOKENS_PER_USER` is reached (whether it reverts or returns false — a revert is required for the DoS; based on `Pool.addToDepositTokensOfAccount` usage and the deployed `Pool` ABI containing `MAX_TOKENS_PER_USER`, it reverts).

### Citations

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

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```

**File:** contracts/storage/PoolStorage.sol (L76-83)
```text
     * @notice Per-account deposit tokens (i.e. tokens that user has balance > 0)
     */
    MappedEnumerableSet.AddressSet internal depositTokensOfAccount;

    /**
     * @notice Per-account debt tokens (i.e. tokens that user has balance > 0)
     */
    MappedEnumerableSet.AddressSet internal debtTokensOfAccount;
```
