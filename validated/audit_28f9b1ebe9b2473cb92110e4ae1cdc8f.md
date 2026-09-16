### Title
Missing reference-counting on content-addressed cache eviction causes use of stale/missing Trie nodes in `TrieImpl` (account-state Merkle trie) — ([File: framework/src/main/java/org/tron/core/trie/TrieImpl.java])

### Summary
`TrieImpl.Node.dispose()` unconditionally evicts a node's content-hash entry from the shared, persistent node cache/store whenever the node is compacted, replaced, or deleted during `insert()`/`delete()`, without any check for other still-valid references to that same hash. Because the trie is content-addressed (nodes are keyed purely by RLP hash) and the same `cache`/store instance (`AccountStateStoreTrie`) is shared across many independently-constructed `TrieImpl` instances (one per block being executed, plus ad-hoc instances built for arbitrary historical/solidity roots), a mutation in one trie context can delete a cache entry that another, still-valid trie root still needs to resolve. This mirrors the UBIFS class of bug: a cached back-reference to a tree node is invalidated on tree restructuring/deletion without accounting for other live consumers of that reference, so a later access dereferences something that is no longer there.

### Finding Description
`TrieImpl.Node.dispose()` is defined as: [1](#0-0) 

It is called on every structural mutation of the trie — compaction of a branch node, merging of KV nodes, and node deletion — inside `insert()`: [2](#0-1) 
and `delete()`: [3](#0-2) 

`dispose()` calls `deleteHash(hash)`, which removes the hash entry from the shared `cache`/store (`cache.remove(hash)`): [4](#0-3) 

Later resolution of *any* node referencing that same hash goes through `resolveCheck()`/`resolve()`, which throws an unhandled `RuntimeException` if the hash is no longer present: [5](#0-4) 

Crucially, `TrieImpl` instances are not isolated: the *same* backing store (`AccountStateStoreTrie`, which is itself a persistent `TronStoreWithRevoking<BytesCapsule>` shared across the whole node) is used to construct a fresh `TrieImpl` per block during execution, and again to construct ad-hoc `TrieImpl` instances for arbitrary historical roots when serving account-state queries: [6](#0-5) [7](#0-6) 

Since Merkle-Patricia nodes are addressed purely by content hash, unrelated (key, value) subtrees that happen to encode to byte-identical RLP across different blocks/roots will collapse to the *same* hash entry in this shared store. There is no reference count on that shared entry: a mutation in one block's trie (e.g. compacting or deleting a node with a colliding hash) calls `dispose()` and evicts the entry from the store entirely, even though another still-referenced historical root (e.g. the solidified account-state root used by `AccountStateStoreTrie.getSolidityAccount()` / `TrieService.getSolidityAccountStateRootHash()`) still depends on that hash being resolvable. When that other root is later resolved, `resolveCheck()` fails and `resolve()` throws, propagating an unhandled `RuntimeException` out of code paths executed during ordinary block processing (`AccountStateCallBack.executePushFinish()`/`executeGenerateFinish()`) or account-state query paths (`AccountStateStoreTrie.getAccount(key, rootHash)`).

This is the same root-cause class as the UBIFS CVE: a cached structural back-reference (`znode->cparent` there, the content-hash cache entry here) is torn down as a side effect of tree mutation/deletion without accounting for the fact that another live part of the data structure (a node that "became root" in UBIFS; a still-referenced historical root here) still depends on it, leading to use of something that no longer exists.

### Impact Explanation
When the eviction races with, or precedes, resolution of a still-valid historical/solidity account-state root, the node throws an unhandled `RuntimeException("Invalid Trie state, can't resolve hash ...")`. Depending on where this surfaces:
- During block push/commit (`AccountStateCallBack.executePushFinish()` / `executeGenerateFinish()`), it can abort block application and threaten node availability/consensus for any node running with the account-state-root feature enabled.
- During account-state proof queries (`AccountStateStoreTrie.getAccount(key, rootHash)`, reachable from the account-state API surface backing `TrieService`), it causes that API to become permanently unable to serve valid historical queries for the affected hash, i.e., "an API the node can no longer serve."

### Likelihood Explanation
This requires the account-state Merkle trie feature (`ALLOW_ACCOUNT_STATE_ROOT`) to be enabled, and requires content-hash collisions between structurally distinct trie nodes across different block roots — which is a natural property of a content-addressed trie holding many identical/repeated (key, value) states (e.g., repeated zero-balance or otherwise-identical account records), and could plausibly be engineered deliberately by an attacker crafting transactions that produce colliding node encodings. I was not able to fully confirm within the available tool budget (a) the exact call sites in `Manager.java` that invoke `AccountStateCallBack.exeTransFinish()`/`deleteAccount()` per transaction, or (b) whether `ALLOW_ACCOUNT_STATE_ROOT` is enabled by default on mainnet — these would materially affect real-world exploitability and should be verified directly in the repository.

### Recommendation
Add reference counting (or copy-on-write duplication) for shared content-hash cache entries in `TrieImpl`/`AccountStateStoreTrie` so that `dispose()` only evicts a hash entry when no other live `Node`/root still references it, or avoid deleting shared entries eagerly and instead rely on garbage collection at snapshot/prune time with proper liveness analysis across all retained roots (current, solidified, and any externally referenced historical roots).

### Proof of Concept
Not independently reproduced against the running node within this analysis; a concrete PoC would need to (1) enable `ALLOW_ACCOUNT_STATE_ROOT`, (2) craft two blocks whose account-state tries produce a byte-identical RLP-encoded node (same hash) at different logical positions, (3) trigger a delete/compaction of that node in one block's trie via a transaction, and (4) subsequently query the account-state proof for the other, still-valid root and observe the `RuntimeException` thrown from `Node.resolve()`.

### Citations

**File:** framework/src/main/java/org/tron/core/trie/TrieImpl.java (L107-109)
```java
  private void deleteHash(byte[] hash) {
    cache.remove(hash);
  }
```

**File:** framework/src/main/java/org/tron/core/trie/TrieImpl.java (L188-204)
```java
      } else if (commonPrefix.isEmpty()) {
        Node newBranchNode = new Node();
        insert(newBranchNode, currentNodeKey, n.kvNodeGetValueOrNode());
        insert(newBranchNode, k, nodeOrValue);
        n.dispose();
        return newBranchNode;
      } else if (commonPrefix.equals(currentNodeKey)) {
        insert(n.kvNodeGetChildNode(), k.shift(commonPrefix.getLength()), nodeOrValue);
        return n.invalidate();
      } else {
        Node newBranchNode = new Node();
        Node newKvNode = new Node(commonPrefix, newBranchNode);
        // TODO can be optimized
        insert(newKvNode, currentNodeKey, n.kvNodeGetValueOrNode());
        insert(newKvNode, k, nodeOrValue);
        n.dispose();
        return newKvNode;
```

**File:** framework/src/main/java/org/tron/core/trie/TrieImpl.java (L243-286)
```java
      // only value or a single child left - compact branch node to kvNode
      n.dispose();
      if (compactIdx == 16) { // only value left
        return new Node(TrieKey.empty(true), n.branchNodeGetValue());
      } else { // only single child left
        newKvNode = new Node(TrieKey.singleHex(compactIdx), n.branchNodeGetChild(compactIdx));
      }
    } else { // n - kvNode
      TrieKey k1 = k.matchAndShift(n.kvNodeGetKey());
      if (k1 == null) {
        // no key found
        return n;
      } else if (type == NodeType.KVNodeValue) {
        if (k1.isEmpty()) {
          // delete this kvNode
          n.dispose();
          return null;
        } else {
          // else no key found
          return n;
        }
      } else {
        Node newChild = delete(n.kvNodeGetChildNode(), k1);
        if (newChild == null) {
          throw new RuntimeException("Shouldn't happen");
        }
        newKvNode = n.kvNodeSetValueOrNode(newChild);
      }
    }

    // if we get here a new kvNode was created, now need to check
    // if it should be compacted with child kvNode
    Node newChild = newKvNode.kvNodeGetChildNode();
    if (newChild.getType() != NodeType.BranchNode) {
      // two kvNodes should be compacted into a single one
      TrieKey newKey = newKvNode.kvNodeGetKey().concat(newChild.kvNodeGetKey());
      Node newNode = new Node(newKey, newChild.kvNodeGetValueOrNode());
      newChild.dispose();
      newKvNode.dispose();
      return newNode;
    } else {
      // no compaction needed
      return newKvNode;
    }
```

**File:** framework/src/main/java/org/tron/core/trie/TrieImpl.java (L629-642)
```java
    public boolean resolveCheck() {
      if (rlp != null || parsedRlp != null || hash == null) {
        return true;
      }
      rlp = getHash(hash);
      return rlp != null;
    }

    private void resolve() {
      if (!resolveCheck()) {
        logger.error("Invalid Trie state, can't resolve hash " + toHexString(hash));
        throw new RuntimeException("Invalid Trie state, can't resolve hash " + toHexString(hash));
      }
    }
```

**File:** framework/src/main/java/org/tron/core/trie/TrieImpl.java (L897-901)
```java
    public void dispose() {
      if (hash != null) {
        deleteHash(hash);
      }
    }
```

**File:** framework/src/main/java/org/tron/core/db/accountstate/callback/AccountStateCallBack.java (L52-72)
```java
  public void preExecute(BlockCapsule blockCapsule) {
    this.blockCapsule = blockCapsule;
    this.execute = true;
    this.allowGenerateRoot = chainBaseManager.getDynamicPropertiesStore().allowAccountStateRoot();
    if (!exe()) {
      return;
    }
    byte[] rootHash = null;
    try {
      BlockCapsule parentBlockCapsule =
          chainBaseManager.getBlockById(blockCapsule.getParentBlockId());
      rootHash = parentBlockCapsule.getInstance().getBlockHeader().getRawData()
          .getAccountStateRoot().toByteArray();
    } catch (Exception e) {
      logger.error("", e);
    }
    if (Arrays.equals(Internal.EMPTY_BYTE_ARRAY, rootHash)) {
      rootHash = Hash.EMPTY_TRIE_HASH;
    }
    trie = new TrieImpl(db, rootHash);
  }
```

**File:** framework/src/main/java/org/tron/core/db/accountstate/storetrie/AccountStateStoreTrie.java (L35-47)
```java
  public AccountStateEntity getAccount(byte[] key) {
    return getAccount(key, trieService.getFullAccountStateRootHash());
  }

  public AccountStateEntity getAccount(byte[] key, byte[] rootHash) {
    TrieImpl trie = new TrieImpl(this, rootHash);
    byte[] value = trie.get(Hash.encodeElement(key));
    return ArrayUtils.isEmpty(value) ? null : AccountStateEntity.parse(value);
  }

  public AccountStateEntity getSolidityAccount(byte[] key) {
    return getAccount(key, trieService.getSolidityAccountStateRootHash());
  }
```
