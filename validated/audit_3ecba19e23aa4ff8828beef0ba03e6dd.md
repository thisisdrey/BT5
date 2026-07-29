### Title
Unprivileged self-lock on `LoansNFT` permanently blocks the guardian's `forceTransfer` rescue path — ([File: contracts/LoansNFT.sol])

### Summary
`LoansNFT.lock()` can be called by any token owner (or authorized operator) with an arbitrary `unlocker` address, including one they control themselves. `forceTransfer`, the guardian-only rescue mechanism for stuck/fraudulent loan NFTs, unconditionally reverts when a token is locked (`require(_unlockers[tokenId] == address(0), TokenLocked())`). Because only the `unlocker` can clear the lock via `unlock()`, a malicious current owner can front-run (or simply pre-empt) a guardian's `forceTransfer` transaction by locking the token to an address they control, permanently defeating the rescue path — directly analogous to the referenced report where an unprivileged party front-runs a privileged corrective action and deletes/mutates state the privileged call depends on.

### Finding Description
`lock()` authorizes based on `_isAuthorized(tokenOwner, msg.sender, id)` — i.e., the current owner or an approved operator — with no restriction on who the `unlocker` may be: [1](#0-0) 

`forceTransfer` is the guardian-only privileged path explicitly documented as "a rescue path for NFTs stranded in a stuck receiver after settlement," and it hard-reverts if the token is locked: [2](#0-1) 

`unlock()` can only be called by the stored `_unlockers[id]` address: [3](#0-2) 

Confirmed by the project's own test suite, `forceTransfer` reverts with `TokenLocked` whenever a lock is active, and the guardian must first clear the lock (e.g., via `LoansExchange.forceCancelOffer`) before the rescue can proceed: [4](#0-3) [5](#0-4) 

This mirrors the C4 pattern exactly: an unprivileged actor (current token owner) can, at any time and especially by watching the mempool for a guardian `forceTransfer` transaction, submit `lock(collusionAddress, tokenId)` naming an unlocker they control. Because `lock()` has no gate tying the unlocker to a legitimate integrator (like `LoansExchange`), the owner can self-lock outside of any exchange listing, and since they alone control the unlocker, they can refuse to ever call `unlock()`. The guardian's subsequent `forceTransfer` reverts with `TokenLocked`, and — unlike the exchange flow, where `forceCancelOffer` gives the guardian an alternate path to clear the lock — there is no guardian-callable function to clear an arbitrary self-imposed lock. The only recovery is if the colluding unlocker voluntarily calls `unlock()`.

### Impact Explanation
This breaks the intended security invariant that the guardian can always force-transfer a loan NFT out of a stuck/compromised/fraudulent holder as a last-resort recovery. A malicious or compromised investor can permanently and unilaterally neutralize this control by self-locking their token, causing the loan NFT (and the investor entitlements it represents) to become practically unrecoverable via the guardian's designated recovery mechanism — matching the "Permanent or practically unrecoverable lock of ... loan NFTs caused by an unprivileged path" and "Material production DoS ... that blocks ... loan settlement" impact categories.

### Likelihood Explanation
Likelihood is moderate-to-high: `lock()` is a completely public, unprivileged function requiring only NFT ownership/approval and one transaction. No special conditions or races are strictly required to set up the block (the owner can lock at any time in advance, not only by front-running); front-running merely makes the block deterministic and timed against a specific rescue attempt, matching the report's mempool-monitoring scenario.

### Recommendation
Give the guardian (or an admin/guardian-gated path) an explicit override to clear a lock independent of the unlocker, or make `forceTransfer` bypass an arbitrary self-imposed lock instead of unconditionally reverting — e.g., add a guardian-only `forceUnlock(tokenId)` in `LoansNFT`, mirroring the existing `LoansExchange.forceCancelOffer` guardian recovery pattern, so the rescue path cannot be permanently vetoed by the very party it is meant to act against.

### Proof of Concept
1. Investor `A` owns loan NFT `id` (`loansNFT.ownerOf(id) == A`).
2. Guardian detects fraud/compromise and prepares `loansNFT.forceTransfer(A, rescueAddress, id)`.
3. `A` observes the pending transaction in the mempool and front-runs it with `loansNFT.lock(A_colludingAddress, id)` (a plain unprivileged call, not via `LoansExchange`).
4. Guardian's `forceTransfer` executes and reverts with `TokenLocked` (as demonstrated in `test_ForceTransfer_Reverts_WhenTokenIsLocked`, `Kohvert/2026-07-tare-dev-oyakhil-main--009/tare-io__tare-contracts/test/LoansNFT/unit/LoansNFT_ForceTransfer.t.sol:72-86`).
5. `A_colludingAddress` never calls `unlock(id)`. The guardian has no alternate mechanism to clear this arbitrary lock (unlike `LoansExchange.forceCancelOffer` for exchange-originated locks), so the NFT — and the investor position it represents — is permanently shielded from the guardian's rescue path.

### Citations

**File:** tare-io__tare-contracts/contracts/LoansNFT.sol (L61-77)
```text
  function forceTransfer(address from, address to, uint256 tokenId) external {
    require(IGuardianAccessControl(LOANS_CONTRACT).hasRole(GUARDIAN_ROLE, msg.sender), Unauthorized());

    address currentOwner = _requireOwned(tokenId);
    require(from == currentOwner, InvalidFrom());
    require(to != address(0), InvalidTo());
    require(_unlockers[tokenId] == address(0), TokenLocked());

    // Pass `address(0)` as `auth` to bypass the ERC721 approval check. The
    // override still runs (bumping ownership nonces and emitting `Transfer`).
    _update(to, tokenId, address(0));

    // Ensure `to` can receive ERC-721s, mirroring the safe-transfer rescue path.
    ERC721Utils.checkOnERC721Received(msg.sender, from, to, tokenId, "");

    emit ForceTransfer(from, to, tokenId);
  }
```

**File:** tare-io__tare-contracts/contracts/LoansNFT.sol (L86-98)
```text
  function lock(address unlocker, uint256 id) external {
    address tokenOwner = ownerOf(id);

    require(unlocker != address(0), InvalidUnlocker());
    require(_unlockers[id] == address(0), AlreadyLocked());
    require(_isAuthorized(tokenOwner, msg.sender, id), Unauthorized());

    // Clear approval
    _approve(address(0), id, address(0), false);
    _unlockers[id] = unlocker;

    emit Lock(unlocker, id);
  }
```

**File:** tare-io__tare-contracts/contracts/LoansNFT.sol (L100-108)
```text
  /// @inheritdoc ILockable
  function unlock(uint256 id) external {
    _requireOwned(id);
    require(msg.sender == _unlockers[id], NotUnlocker());

    delete _unlockers[id];

    emit Unlock(id);
  }
```

**File:** tare-io__tare-contracts/test/LoansNFT/unit/LoansNFT_ForceTransfer.t.sol (L72-86)
```text
  function test_ForceTransfer_Reverts_WhenTokenIsLocked() public {
    uint64 id = _newLoan();
    address unlocker = makeAddr("unlocker");

    vm.prank(investor);
    loansNFT.lock(unlocker, uint256(id));
    assertEq(loansNFT.getLocked(uint256(id)), unlocker);

    vm.prank(guardian);
    vm.expectRevert(ILockable.TokenLocked.selector);
    loansNFT.forceTransfer(investor, newOwner, uint256(id));

    assertEq(loansNFT.ownerOf(id), investor);
    assertEq(loansNFT.getLocked(uint256(id)), unlocker);
  }
```

**File:** tare-io__tare-contracts/test/LoansExchange/integration/LoansExchange_ForceTransferRescue.t.sol (L22-31)
```text
    // 2. Guardian's first forceTransfer attempt reverts: the exchange lock
    //    must be cleared before the rescue can proceed.
    vm.prank(guardian);
    vm.expectRevert(ILockable.TokenLocked.selector);
    loansNFT.forceTransfer(seller, newOwner, uint256(loanId));

    // 3. Guardian force-cancels the offer; the exchange unlocks the loan.
    vm.prank(guardian);
    exchange.forceCancelOffer(offerId);
    assertEq(loansNFT.getLocked(uint256(loanId)), address(0));
```
