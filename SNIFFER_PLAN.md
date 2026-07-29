# Conflux 主网区块传播窃听节点 - 完整计划

## 1. 项目目标

构建一个 Conflux 主网"窃听"节点，用于调查新区块的来源 IP 地址。

### 1.1 核心功能

1. **最大化连接数**：尽可能多地连接 Conflux 主网的其他全节点
2. **记录 NewBlockHash 首达 IP**：当任何新区块的 `NewBlockHash` 消息到达时，精确记录是哪个 IP 地址最先发出的
3. **记录区块元数据**：保存区块哈希、区块高度、时间戳、difficulty、nonce 等信息，便于后续分析异常区块
4. **不做任何同步工作**：不请求区块体、不验证区块、不参与共识、不存储状态、不出块、不转发交易

---

## 2. 方案选择：直接修改 Conflux-Rust

### 2.1 为什么不能另起仓库

用户提出了另起仓库调用 Conflux crates 的想法。经过对源代码的详细分析，确认此方案不可行，原因如下：

#### 问题一：无法绕过 cfxcore 的 God Crate 问题

`SynchronizationProtocolHandler` 是 cfxcore 的核心类型，其结构体持有以下字段的直接引用：

- `graph: SharedSynchronizationGraph` — 依赖 `ConsensusGraph`、`BlockDataManager`
- `request_manager: Arc<RequestManager>` — 依赖 `SynchronizationState`、消息队列
- `syn: SynchronizationState` — 依赖 peer 状态管理、heartbeat 机制
- `phase_manager: SynchronizationPhaseManager` — 依赖阶段转换逻辑
- `light_provider` — 依赖轻节点提供者

这些类型之间形成了紧密的相互引用关系。例如 `produce_status_message_v2()` 调用 `self.graph.consensus.best_info()`，`broadcast_heartbeat()` 同时调用 `produce_status_message_v2()` 和 `produce_heartbeat_message()`，两者都依赖 `ConsensusGraph` 的内部状态。要将 `SynchronizationProtocolHandler` 单独提取到新仓库，必须同时搬移 ConsensusGraph、BlockDataManager、RequestManager、SynchronizationState 等全部依赖链，这等同于搬移整个 cfxcore。

#### 问题二：NetworkProtocolHandler trait 的方法签名依赖

`NetworkProtocolHandler` trait 定义在 `crates/network/src/lib.rs` 第 242 行：

```rust
pub trait NetworkProtocolHandler: Sync + Send {
    fn on_peer_connected(
        &self, io: &dyn NetworkContext, node_id: &NodeId,
        peer_protocol_version: ProtocolVersion,
        pos_public_key: Option<(ConsensusPublicKey, ConsensusVRFPublicKey)>,
    );
    fn on_message(&self, io: &dyn NetworkContext, node_id: &NodeId, data: &[u8]);
    fn on_peer_disconnected(&self, io: &dyn NetworkContext, node_id: &NodeId);
    fn on_timeout(&self, io: &dyn NetworkContext, timer: TimerToken);
    // ...
}
```

trait 本身使用的 `ConsensusPublicKey` 和 `ConsensusVRFPublicKey` 来自 `diem_types`，是 network crate 自身的依赖（`crates/network/src/lib.rs` 第 47 行），不需要依赖 cfxcore。但问题在于，`on_message` 接收的 `data: &[u8]` 需要调用 `decode_msg()`（定义在 `crates/cfxcore/core/src/message.rs` 第 145 行）才能解析出 `MsgId` 和 `Rlp`，而所有消息类型（`StatusV2`、`StatusV3`、`NewBlockHashes`、`NewBlock`、`Heartbeat` 等）全部定义在 cfxcore 中。在新仓库中实现 `on_message` 意味着要么重新实现所有消息类型的 RLP 编解码，要么仍然依赖 cfxcore。

#### 问题三：Context 结构体的硬依赖

`Context` 定义在 `crates/cfxcore/core/src/sync/message/handleable.rs` 第 13 行：

```rust
pub struct Context<'a> {
    pub io: &'a dyn NetworkContext,
    pub node_id: NodeId,
    pub manager: &'a SynchronizationProtocolHandler,
}
```

`Handleable` trait 依赖 `Context`：

```rust
pub trait Handleable {
    fn handle(self, ctx: &Context) -> Result<(), Error>;
}
```

所有消息处理器（`NewBlockHashes`、`NewBlock`、`Status`、`Heartbeat` 等）都实现 `Handleable` trait，其 `handle()` 方法签名接收 `&Context`。这意味着任何想要复用消息处理逻辑的代码都必须依赖 `SynchronizationProtocolHandler`，无法绕过。

#### 结论

另起仓库无法避免对 cfxcore 的深度依赖。无论是否使用新仓库，最终都需要引入 cfxcore 的大部分代码。相比之下，直接修改 Conflux-Rust 源代码只需添加约 200 行修改，且能复用全部基础设施。

### 2.2 直接修改的优势

| 方面 | 说明 |
|------|------|
| **复用基础设施** | 网络层、消息编解码、Session 管理、Discovery 协议全部复用 |
| **修改量可控** | 通过 `sniffer_mode` 标志做条件分支，不改变原有逻辑路径 |
| **协议兼容性** | 使用与正常节点完全相同的 Status 握手、Heartbeat 机制 |
| **可跟进上游** | 修改集中在少数文件，合并上游更新时冲突极少 |
| **首次编译代价** | 全量编译约 10-15 分钟，但只需一次 |

### 2.3 修改量评估

| 文件 | 修改类型 | 修改量 |
|------|----------|--------|
| `crates/network/src/lib.rs` | 扩展 NetworkContext trait | ~10 行 |
| `crates/network/src/service.rs` | 实现 get_peer_addr | ~15 行 |
| `crates/cfxcore/core/src/sync/message/handleable.rs` | 扩展 Context 结构体 | ~5 行 |
| `crates/cfxcore/core/src/sync/synchronization_protocol_handler.rs` | 核心逻辑（多方法修改） | ~120 行 |
| `crates/cfxcore/core/src/sync/message/new_block_hashes.rs` | 记录逻辑 | ~15 行 |
| `crates/cfxcore/core/src/sync/message/new_block.rs` | 记录逻辑 | ~15 行 |
| `crates/cfxcore/core/src/sync/message/transactions.rs` | 窃听模式守卫 | ~12 行 |
| `crates/cfxcore/core/src/sync/state/snapshot_chunk_sync.rs` | 补充 Context 字段 | ~3 行 |
| `crates/config/src/configuration.rs` | 配置项 | ~10 行 |
| **总计** | | **~205 行** |

---

## 3. 逻辑漏洞分析与解决方案

### 3.1 catch_up_mode 下 NewBlockHash 被忽略

**问题描述**：

在 `crates/cfxcore/core/src/sync/message/new_block_hashes.rs` 中，当 `ctx.manager.catch_up_mode()` 返回 `true` 时，`NewBlockHashes` 消息被直接忽略。新启动的节点默认处于 `CatchUpRecoverBlockHeaderFromDB` 阶段，`catch_up_mode()` 返回 `true`，导致窃听节点永远收不到 NewBlockHash 消息。

**解决方案**：

在 `NewBlockHashes::handle()` 开头添加窃听模式检查，无条件记录后直接返回：

```rust
impl Handleable for NewBlockHashes {
    fn handle(self, ctx: &Context) -> Result<(), Error> {
        // ====== 窃听模式：无条件记录 ======
        if ctx.manager.sniffer_mode {
            for hash in &self.block_hashes {
                ctx.manager.record_block_hash_first_seen(
                    *hash,
                    ctx.peer_addr,
                    ctx.node_id,
                );
            }
            return Ok(());
        }
        // ====== 原有逻辑保持不变 ======
        debug!("on_new_block_hashes, msg={:?}", self);
        if ctx.manager.catch_up_mode() {
            // ... 原有逻辑 ...
        }
        // ...
    }
}
```

### 3.2 NewBlock 处理会触发额外请求

**问题描述**：

在 `crates/cfxcore/core/src/sync/message/new_block.rs` 中，处理 `NewBlock` 消息会调用 `ctx.manager.request_block_headers()`，向对端请求父区块头和引用区块头。这会给其他节点制造不必要的流量压力。

**解决方案**：

在窃听模式下只记录 IP 和区块哈希，不触发任何请求：

```rust
impl Handleable for NewBlock {
    fn handle(self, ctx: &Context) -> Result<(), Error> {
        if ctx.manager.protocol_config.sniffer_mode {
            let hash = self.block.block_header.hash();
            ctx.manager.record_block_hash_first_seen(
                hash,
                ctx.peer_addr,
                ctx.node_id,
            );
            return Ok(());
        }
        // ====== 原有逻辑保持不变 ======
        // ...
    }
}
```

注意：窃听模式下对 `NewBlock` 消息也进行记录（不仅仅是 `NewBlockHashes`），因为 `NewBlock` 包含完整区块头，可以提取 difficulty、nonce、pos_public_key 等字段用于异常分析。

### 3.3 start_sync 会向所有 peer 发送 GetBlockHeaders 请求

**问题描述**：

`start_sync()` 在多个消息处理器中被调用：
- `status.rs` 第 72、127、199、254 行（握手完成后和收到 Status 更新时）
- `heartbeat.rs` 第 33 行（收到 Heartbeat 且 latest_updated 时）
- `get_block_hashes_response.rs` 第 63 行（收到区块哈希响应后）

即使 `catch_up_mode()` 返回 `false`，`start_sync()` 的 `else` 分支（第 745-747 行）仍会调用 `request_missing_terminals()`，该方法遍历所有 peer，对每个 peer 的 `latest_block_hashes` 调用 `request_block_headers()` 发送 `GetBlockHeaders` 请求。这会在每次收到 Heartbeat 或 Status 消息时给所有 peer 制造大量请求流量。

**关键点**：`start_sync()` 内部检查的是 `phase_type` 而非 `catch_up_mode()`，因此仅修改 `catch_up_mode()` 的返回值不足以阻止此行为。

**解决方案**：

在 `start_sync()` 开头添加窃听模式检查：

```rust
pub fn start_sync(&self, io: &dyn NetworkContext) {
    if self.sniffer_mode {
        return;  // 窃听模式不发起任何同步请求
    }
    // ====== 原有逻辑保持不变 ======
    let current_phase_type = self.phase_manager.get_current_phase().phase_type();
    // ...
}
```

### 3.4 broadcast_heartbeat 广播不必要的 StatusV2

**问题描述**：

`broadcast_heartbeat()`（第 1284 行）同时广播两种消息：
1. `Heartbeat` 消息 — 包含 `best_epoch` 和 `terminal_block_hashes`
2. `StatusV2` 消息 — 包含 `chain_id`、`genesis_hash`、`best_epoch`、`terminal_block_hashes`

两者都通过 `self.graph.consensus.best_info()` 获取数据。窃听节点未同步任何区块，`best_epoch` 为 0，`terminal_block_hashes` 为空。虽然这在技术上合法（新节点确实处于 epoch 0），但广播 StatusV2 是不必要的流量，且可能触发对端不必要的同步帮助行为。

**解决方案**：

在窃听模式下只广播 Heartbeat，跳过 StatusV2：

```rust
fn broadcast_heartbeat(&self, io: &dyn NetworkContext) {
    let heartbeat_message = self.produce_heartbeat_message();

    if self.broadcast_message(io, &Default::default(), &heartbeat_message).is_err() {
        warn!("Error broadcasting heartbeat message");
    }

    if !self.sniffer_mode {
        let status_message = self.produce_status_message_v2();
        if self.broadcast_message(io, &Default::default(), &status_message).is_err() {
            warn!("Error broadcasting status message");
        }
    }
}
```

注意：`on_peer_connected` 中的 `send_status()` 必须保留，因为 Status 握手是建立连接的必要条件。窃听节点需要发送正确的 `chain_id`（1029）和 `genesis_hash`（主网创世哈希）才能通过握手验证。

### 3.5 update_sync_phase 触发阶段转换和能力广播

**问题描述**：

`update_sync_phase()`（第 1611 行）由 `CHECK_CATCH_UP_MODE_TIMER` 定时器触发，执行以下操作：
1. 调用 `phase_manager.try_initialize()` 初始化阶段管理器
2. 循环调用 `current_phase.next()` 尝试阶段转换
3. 向所有 peer 广播 `DynamicCapability::NormalPhase` 通知

窃听节点不需要阶段转换，也不应广播能力变更。

**解决方案**：

在 `update_sync_phase()` 开头添加窃听模式检查：

```rust
pub fn update_sync_phase(&self, io: &dyn NetworkContext) {
    if self.sniffer_mode {
        return;  // 窃听模式不执行阶段转换
    }
    // ====== 原有逻辑保持不变 ======
    // ...
}
```

### 3.6 on_timeout 中多个定时器执行不必要的工作

**问题描述**：

`on_timeout()`（第 1906 行）处理多个定时器，其中部分在窃听模式下不必要甚至有害：

| 定时器 | 行为 | 窃听模式处理 |
|--------|------|-------------|
| `TX_TIMER` | 传播新交易 | 跳过（无交易需传播） |
| `CHECK_FUTURE_BLOCK_TIMER` | 检查未来区块 + 调用 consensus graph | 跳过（无区块需检查，避免触发共识逻辑） |
| `CHECK_REQUEST_TIMER` | 清理过期请求 | 保留（无害，维持请求管理器状态一致） |
| `HEARTBEAT_TIMER` | 发送心跳 | 保留（必须，保持连接活跃） |
| `BLOCK_CACHE_GC_TIMER` | 缓存垃圾回收 | 跳过（无缓存数据） |
| `CHECK_CATCH_UP_MODE_TIMER` | 调用 update_sync_phase | 跳过（见 3.5） |
| `LOG_STATISTIC_TIMER` | 日志统计 | 保留（无害，有助于调试） |
| `TOTAL_WEIGHT_IN_PAST_TIMER` | 更新共识权重 | 跳过（无共识数据） |
| `CHECK_PEER_HEARTBEAT_TIMER` | 检查对端心跳超时并断连 | 保留（必须，清理死连接） |
| `EXPIRE_BLOCK_GC_TIMER` | 过期区块清理 | 跳过（无区块数据） |

**解决方案**：

在 `on_timeout()` 中对需要跳过的定时器添加 `sniffer_mode` 检查：

```rust
fn on_timeout(&self, io: &dyn NetworkContext, timer: TimerToken) {
    match timer {
        TX_TIMER => {
            if !self.sniffer_mode {
                self.propagate_new_transactions(io);
            }
        }
        CHECK_FUTURE_BLOCK_TIMER => {
            if !self.sniffer_mode {
                self.check_future_blocks(io);
                self.graph.check_not_ready_frontier(
                    self.insert_header_to_consensus(),
                );
            }
        }
        CHECK_REQUEST_TIMER => {
            self.remove_expired_flying_request(io);
        }
        HEARTBEAT_TIMER => {
            self.send_heartbeat(io);
        }
        BLOCK_CACHE_GC_TIMER => {
            if !self.sniffer_mode {
                self.gc();
            }
        }
        CHECK_CATCH_UP_MODE_TIMER => {
            if !self.sniffer_mode {
                self.update_sync_phase(io);
            }
        }
        LOG_STATISTIC_TIMER => {
            self.log_statistics();
        }
        TOTAL_WEIGHT_IN_PAST_TIMER => {
            if !self.sniffer_mode {
                self.update_total_weight_delta_heartbeat();
            }
        }
        CHECK_PEER_HEARTBEAT_TIMER => {
            let timeout_peers = self.syn.get_heartbeat_timeout_peers(
                self.protocol_config.heartbeat_timeout,
            );
            for peer in timeout_peers {
                io.disconnect_peer(
                    &peer,
                    Some(UpdateNodeOperation::Failure),
                    "sync heartbeat timeout",
                );
            }
        }
        EXPIRE_BLOCK_GC_TIMER => {
            if !self.sniffer_mode {
                self.expire_block_gc(
                    io,
                    self.protocol_config.sync_expire_block_timeout.as_secs(),
                );
            }
        }
        _ => {}
    }
}
```

### 3.7 catch_up_mode 判断逻辑

**问题描述**：

新节点启动时处于 `CatchUpRecoverBlockHeaderFromDB` 阶段，`catch_up_mode()` 返回 `true`。虽然 3.1 中已在 `NewBlockHashes::handle()` 添加了窃听模式提前返回，但 `catch_up_mode()` 的返回值还被其他代码路径使用（如 `relay_blocks`、`propagate_new_transactions` 等），需要在窃听模式下统一返回 `false` 以避免这些路径的异常行为。

**解决方案**：

```rust
pub fn catch_up_mode(&self) -> bool {
    if self.sniffer_mode {
        return false;
    }
    self.phase_manager.get_current_phase().phase_type()
        != SyncPhaseType::Normal
}
```

注意：仅修改 `catch_up_mode()` 不足以阻止 `start_sync()` 发送请求（见 3.3），因为 `start_sync()` 内部直接检查 `phase_type` 而非调用 `catch_up_mode()`。两者必须同时修改。

### 3.8 同步文件 I/O 阻塞 IO 线程

**问题描述**：

`write_sniffer_record()` 在网络 IO 线程中执行同步文件写入（`OpenOptions::new().append(true).open()` + `writeln!`）。在高消息率场景下（主网每秒可能收到数十条 NewBlockHash 消息），同步 I/O 会阻塞消息处理线程，导致心跳超时或消息积压。

**解决方案**：

使用 channel 将记录发送到专用写入线程，IO 线程只做非阻塞的 channel send：

```rust
// 在 SynchronizationProtocolHandler 初始化时创建 channel 和写入线程
pub sniffer_writer_tx: Option<std::sync::mpsc::Sender<BlockFirstSeen>>,

// 初始化时启动写入线程
fn start_sniffer_writer(log_path: &str) -> std::sync::mpsc::Sender<BlockFirstSeen> {
    let (tx, rx) = std::sync::mpsc::channel::<BlockFirstSeen>();
    let path = log_path.to_string();
    std::thread::spawn(move || {
        let mut file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .expect("Failed to open sniffer log file");
        for record in rx {
            let json = serde_json::json!({
                "block_hash": format!("{:?}", record.block_hash),
                "first_peer_ip": record.first_peer_ip.map(|ip| ip.to_string()),
                "first_peer_node_id": format!("{:?}", record.first_peer_node_id),
                "first_seen_at_ms": record.first_seen_at
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_millis() as u64,
            });
            let _ = writeln!(file, "{}", json);
        }
    });
    tx
}

// record_block_hash_first_seen 中使用非阻塞 send
pub fn record_block_hash_first_seen(
    &self, hash: H256, peer_addr: Option<SocketAddr>, node_id: NodeId,
) {
    let is_new = self.block_first_seen.write().check_and_insert(hash);
    if is_new {
        let entry = BlockFirstSeen { /* ... */ };

        info!(
            "[SNIFFER] NEW_BLOCK_HASH first seen: hash={:?}, ip={:?}, node_id={:?}",
            hash, peer_addr.map(|a| a.ip()), node_id
        );

        // 非阻塞发送到写入线程
        if let Some(tx) = &self.sniffer_writer_tx {
            let _ = tx.send(entry);
        }
    }
}
```

### 3.9 GET_BLOCK_HEADERS 等请求的响应

**问题描述**：

其他节点可能向窃听节点请求区块头、区块体等数据。不响应会导致对端请求超时，长期可能影响对端对我们的评价。

**解决方案**：

现有代码已正确处理此情况。`get_block_headers.rs` 中的处理逻辑通过 `ctx.manager.graph.data_man.block_header_by_hash()` 查询本地数据，查询结果为空时自然返回空响应。窃听节点本地无区块数据，所有查询都返回空，不会给对端造成额外压力。此路径无需修改。

### 3.10 Status 握手兼容性

**问题描述**：

连接建立时双方交换 Status 消息，`chain_id` 或 `genesis_hash` 不匹配会导致断连。

**解决方案**：

窃听节点使用与正常节点完全相同的 Status 响应逻辑，无需修改。`on_peer_connected` 调用 `send_status()`，后者通过 `produce_status_message_v3()` 或 `produce_status_message_v2()` 构造 Status 消息，其中 `chain_id` 来自 `best_info.best_chain_id()`（在正确初始化 genesis block 后即为 1029），`genesis_hash` 来自 `self.graph.data_man.true_genesis.hash()`（主网创世哈希）。

### 3.11 心跳超时断连

**问题描述**：

Conflux 使用心跳机制检测连接活跃性，默认超时时间 180 秒，检查频率每 60 秒一次。如果 180 秒内没有收到对方的任何消息，会主动断连。

**解决方案**：

窃听节点正常参与心跳交互。`HEARTBEAT_TIMER` 定时器触发 `send_heartbeat()`，该方法调用 `broadcast_heartbeat()` 向所有 peer 发送 Heartbeat 消息。`CHECK_PEER_HEARTBEAT_TIMER` 定时器检查对端心跳超时并清理死连接。这两个定时器在窃听模式下保留。Heartbeat 是单向广播，不需要对方响应。只要双方在 180 秒内有消息交互（包括 Heartbeat、NewBlockHashes 等），连接就不会断开。

### 3.12 PoS 公钥问题

**问题描述**：

用户担心 PoS 公钥是否影响握手。

**分析结果**：

`SessionManager` 中的 `self_pos_public_key` 字段类型为 `Option<(ConsensusPublicKey, ConsensusVRFPublicKey)>`，可以设为 `None`。`on_peer_connected` 的参数 `_pos_public_key` 带下划线前缀，表示对端的 PoS 公钥在当前处理器中未使用。因此窃听节点不需要生成自己的 PoS 密钥对，也不需要处理对端的 PoS 公钥。此路径无需修改。

---

## 4. 详细修改计划

### 步骤 1：扩展 NetworkContext trait

**文件**：`crates/network/src/lib.rs`

在 `NetworkContext` trait 中添加获取 peer IP 地址的方法：

```rust
pub trait NetworkContext {
    // ... 现有方法保持不变 ...

    /// 获取指定 peer 的远程 IP 地址
    fn get_peer_addr(&self, node_id: &NodeId) -> Option<SocketAddr>;
}
```

**原理**：当前 `NetworkContext` 没有提供获取 peer IP 的方法。IP 地址存储在网络层的 `Session` 结构中，需要通过 `SessionManager::get_by_id(node_id)` 获取。添加此方法后，消息处理层可以在 `dispatch_message` 中获取发送方的 IP 地址。

### 步骤 2：实现 get_peer_addr

**文件**：`crates/network/src/service.rs`

在 `NetworkContextImpl` 的 `NetworkContext` trait 实现中添加：

```rust
fn get_peer_addr(&self, node_id: &NodeId) -> Option<SocketAddr> {
    self.host.sessions.get_by_id(node_id)
        .map(|s| s.read().address())
}
```

**原理**：`NetworkContextImpl` 持有对 `NetworkServiceInner` 的引用（`host` 字段），可以通过 `sessions` 字段获取 `Session`，再调用 `address()` 获取 `SocketAddr`。

### 步骤 3：扩展 Context 结构体

**文件**：`crates/cfxcore/core/src/sync/message/handleable.rs`

```rust
use std::net::SocketAddr;

pub struct Context<'a> {
    pub io: &'a dyn NetworkContext,
    pub node_id: NodeId,
    pub manager: &'a SynchronizationProtocolHandler,
    pub peer_addr: Option<SocketAddr>,  // 新增：发送方的 IP 地址
}
```

### 步骤 4：在所有 Context 构造点填充 peer_addr

`Context` 在以下 3 处构造，均需更新：

**文件 1**：`crates/cfxcore/core/src/sync/synchronization_protocol_handler.rs` 第 577 行（`dispatch_message` 方法）

```rust
let ctx = Context {
    node_id: *peer,
    io,
    manager: self,
    peer_addr: io.get_peer_addr(peer),  // 新增
};
```

**文件 2**：`crates/cfxcore/core/src/sync/synchronization_protocol_handler.rs` 第 1022 行（`on_local_message_task` 方法）

```rust
let ctx = Context {
    node_id: io.self_node_id(),
    io,
    manager: self,
    peer_addr: None,  // 本地消息无对端 IP
};
```

**文件 3**：`crates/cfxcore/core/src/sync/state/snapshot_chunk_sync.rs` 第 382 行

```rust
&Context {
    node_id: Default::default(),
    io,
    manager: sync_handler,
    peer_addr: None,  // 新增
},
```

### 步骤 5：添加窃听模式配置

**文件**：`crates/config/src/configuration.rs`

在 `build_config!` 宏中添加：

```rust
(sniffer_mode, (bool), false)
(sniffer_log_file, (String), "sniffer_records.jsonl".to_string())
```

在 `Configuration` impl 中添加访问方法：

```rust
pub fn sniffer_mode(&self) -> bool { self.raw_conf.sniffer_mode }
pub fn sniffer_log_file(&self) -> &str { &self.raw_conf.sniffer_log_file }
```

### 步骤 6：在 SynchronizationProtocolHandler 中添加窃听模式字段

**文件**：`crates/cfxcore/core/src/sync/synchronization_protocol_handler.rs`

在结构体中添加字段：

```rust
pub struct SynchronizationProtocolHandler {
    // ... 现有字段保持不变 ...
    pub sniffer_mode: bool,
    block_first_seen: Arc<RwLock<BlockFirstSeenTracker>>,
    pub sniffer_writer_tx: Option<std::sync::mpsc::Sender<BlockFirstSeen>>,
}
```

添加数据结构：

```rust
const SNIFFER_MAX_SEEN_BLOCKS: usize = 50_000;

struct BlockFirstSeenTracker {
    seen: HashSet<H256>,
    order: VecDeque<H256>,
    max_size: usize,
}

impl BlockFirstSeenTracker {
    fn new() -> Self {
        Self {
            seen: HashSet::with_capacity(SNIFFER_MAX_SEEN_BLOCKS),
            order: VecDeque::with_capacity(SNIFFER_MAX_SEEN_BLOCKS),
            max_size: SNIFFER_MAX_SEEN_BLOCKS,
        }
    }

    fn check_and_insert(&mut self, hash: H256) -> bool {
        if !self.seen.insert(hash) {
            return false;
        }
        self.order.push_back(hash);
        while self.seen.len() > self.max_size {
            if let Some(old) = self.order.pop_front() {
                self.seen.remove(&old);
            } else {
                break;
            }
        }
        true
    }
}

#[derive(Debug, Clone)]
pub struct BlockFirstSeen {
    pub block_hash: H256,
    pub first_peer_ip: Option<IpAddr>,
    pub first_seen_at: SystemTime,
    pub first_peer_node_id: NodeId,
}
```

### 步骤 7：实现记录方法和写入线程

**文件**：`crates/cfxcore/core/src/sync/synchronization_protocol_handler.rs`

```rust
impl SynchronizationProtocolHandler {
    /// 启动写入线程（在 new() 中根据 sniffer_mode 配置调用）
    fn start_sniffer_writer(
        log_path: &str,
    ) -> std::sync::mpsc::Sender<BlockFirstSeen> {
        let (tx, rx) = std::sync::mpsc::channel::<BlockFirstSeen>();
        let path = log_path.to_string();
        std::thread::spawn(move || {
            let mut file = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&path)
                .expect("Failed to open sniffer log file");
            for record in rx {
                let json = serde_json::json!({
                    "block_hash": format!("{:?}", record.block_hash),
                    "first_peer_ip": record.first_peer_ip.map(|ip| ip.to_string()),
                    "first_peer_node_id": format!("{:?}", record.first_peer_node_id),
                    "first_seen_at_ms": record.first_seen_at
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_millis() as u64,
                });
                let _ = writeln!(file, "{}", json);
            }
        });
        tx
    }

    /// 记录区块哈希的首达 IP（非阻塞）
    pub fn record_block_hash_first_seen(
        &self, hash: H256, peer_addr: Option<SocketAddr>, node_id: NodeId,
    ) {
        let is_new = self.block_first_seen.write().check_and_insert(hash);
        if is_new {
            let entry = BlockFirstSeen {
                block_hash: hash,
                first_peer_ip: peer_addr.map(|a| a.ip()),
                first_seen_at: SystemTime::now(),
                first_peer_node_id: node_id,
            };

            info!(
                "[SNIFFER] NEW_BLOCK_HASH first seen: hash={:?}, ip={:?}, node_id={:?}",
                hash, peer_addr.map(|a| a.ip()), node_id
            );

            if let Some(tx) = &self.sniffer_writer_tx {
                let _ = tx.send(entry);
            }
        }
    }
}
```

### 步骤 8：修改 catch_up_mode

**文件**：`crates/cfxcore/core/src/sync/synchronization_protocol_handler.rs`

```rust
pub fn catch_up_mode(&self) -> bool {
    if self.sniffer_mode {
        return false;
    }
    self.phase_manager.get_current_phase().phase_type()
        != SyncPhaseType::Normal
}
```

### 步骤 9：修改 start_sync

**文件**：`crates/cfxcore/core/src/sync/synchronization_protocol_handler.rs` 第 727 行

```rust
pub fn start_sync(&self, io: &dyn NetworkContext) {
    if self.sniffer_mode {
        return;
    }
    // ====== 原有逻辑保持不变 ======
    let current_phase_type = self.phase_manager.get_current_phase().phase_type();
    // ...
}
```

### 步骤 10：修改 broadcast_heartbeat

**文件**：`crates/cfxcore/core/src/sync/synchronization_protocol_handler.rs` 第 1284 行

```rust
fn broadcast_heartbeat(&self, io: &dyn NetworkContext) {
    let heartbeat_message = self.produce_heartbeat_message();
    debug!("Broadcasting heartbeat message: {:?}", heartbeat_message);

    if self.broadcast_message(io, &Default::default(), &heartbeat_message).is_err() {
        warn!("Error broadcasting heartbeat message");
    }

    // 窃听模式跳过 StatusV2 广播
    if !self.sniffer_mode {
        let status_message = self.produce_status_message_v2();
        if self.broadcast_message(io, &Default::default(), &status_message).is_err() {
            warn!("Error broadcasting status message");
        }
    }
}
```

### 步骤 11：修改 update_sync_phase

**文件**：`crates/cfxcore/core/src/sync/synchronization_protocol_handler.rs` 第 1611 行

```rust
pub fn update_sync_phase(&self, io: &dyn NetworkContext) {
    if self.sniffer_mode {
        return;
    }
    // ====== 原有逻辑保持不变 ======
    // ...
}
```

### 步骤 12：修改 on_timeout

**文件**：`crates/cfxcore/core/src/sync/synchronization_protocol_handler.rs` 第 1906 行

按 3.6 节的表格，对 `TX_TIMER`、`CHECK_FUTURE_BLOCK_TIMER`、`BLOCK_CACHE_GC_TIMER`、`CHECK_CATCH_UP_MODE_TIMER`、`TOTAL_WEIGHT_IN_PAST_TIMER`、`EXPIRE_BLOCK_GC_TIMER` 添加 `if !self.sniffer_mode` 守卫。保留 `CHECK_REQUEST_TIMER`、`HEARTBEAT_TIMER`、`LOG_STATISTIC_TIMER`、`CHECK_PEER_HEARTBEAT_TIMER` 的原有逻辑。

### 步骤 13：修改 NewBlockHashes 处理逻辑

**文件**：`crates/cfxcore/core/src/sync/message/new_block_hashes.rs`

按 3.1 节的方案，在 `handle()` 开头添加窃听模式记录逻辑。

### 步骤 14：修改 NewBlock 处理逻辑

**文件**：`crates/cfxcore/core/src/sync/message/new_block.rs`

按 3.2 节的方案，在 `handle()` 开头添加窃听模式记录逻辑，同时记录区块头信息。

### 步骤 15：修改 on_work_dispatch

**文件**：`crates/cfxcore/core/src/sync/synchronization_protocol_handler.rs` 第 1858 行

```rust
fn on_work_dispatch(&self, io: &dyn NetworkContext, work_type: HandlerWorkType) {
    if self.sniffer_mode {
        return;  // 窃听模式不处理任何工作分发
    }
    // ====== 原有逻辑保持不变 ======
    // ...
}
```

### 步骤 16：修改 TRANSACTIONS 和 TransactionDigests 处理逻辑

**文件**：`crates/cfxcore/core/src/sync/message/transactions.rs`

**问题**：`TRANSACTIONS` 消息处理器在第 85 行检查 `!ctx.manager.catch_up_mode()`，由于窃听模式让 `catch_up_mode()` 返回 `false`，条件为 `true`，会执行 `tx_pool().insert_new_transactions()` 将交易插入交易池。这会触发交易的 nonce、余额等状态验证，而窃听节点的状态不正确，可能导致 panic 或异常行为。`TransactionDigests` 处理器（第 128 行）存在类似问题。

**解决方案**：在两个处理器开头添加窃听模式检查：

```rust
// Transactions::handle() 第 49 行
impl Handleable for Transactions {
    fn handle(self, ctx: &Context) -> Result<(), Error> {
        if ctx.manager.sniffer_mode {
            return Ok(());  // 窃听模式忽略所有交易消息
        }
        // ====== 原有逻辑保持不变 ======
        // ...
    }
}

// TransactionDigests::handle() 第 128 行
impl Handleable for TransactionDigests {
    fn handle(self, ctx: &Context) -> Result<(), Error> {
        if ctx.manager.sniffer_mode {
            return Ok(());
        }
        // ====== 原有逻辑保持不变 ======
        // ...
    }
}
```

---

## 5. 配置文件

文件名：`sniffer.toml`

```toml
# Conflux 主网窃听节点配置

# 节点类型
node_type = "full"

# 主网 chain_id
chain_id = 1029
evm_chain_id = 1030

# 启用发现
enable_discovery = true

# 大量连接
max_incoming_peers = 256
max_outgoing_peers = 256
max_handshakes = 128

# 放宽 IP 限制（允许同一子网更多连接）
session_ip_limits = "0,0,0,0"
subnet_quota = 0

# 窃听模式（核心配置）
sniffer_mode = true
sniffer_log_file = "sniffer_records.jsonl"

# Bootstrap 节点（主网官方节点）
bootnodes = "cfxnode://25265e1aa470d9d8667947820c4830a64e9f9678d6cb23ecde91e0447527f4926257b9637923a305ce91e15c929ed28164e6c32b76213764eb4a9624120ae1d7@39.97.180.246:32323,cfxnode://2b72adc3f52a80945db10fa35c3f6d02c73f65ff98b4a9eae4f7b244e8a51f01690e7dcef7a30bfb67fb07fcb2949e67c27487169623d40f6a9e55a8d04ca34f@39.107.143.220:32323,cfxnode://5da942ac58e392e9f68784876a1800ffe5756f8498aa1a7a9a869fe9370c2e838a114dfce33fff9674633700a0094aed8b46722fb6b03619842602a2473223de@39.97.170.199:32323,cfxnode://28d3cdf07b7deb41bb52dee0a952fc599f46f6b89cc513ecfd1020d5a66e73e7cfe68543e64962aefbcae7123a6c390a43144f5900f0bc181c3c89ffdf9ff81b@39.97.225.254:32323,cfxnode://49ff58db6b4c5f92c2145e69ea0625134cbe35885f0e5979191ba9c67e4c9374234ed7fbeb65f82d4d197568110a4f100f078bfbac896f391b362bec77be19ea@39.103.68.228:32323,cfxnode://97497107e94ac463f6bad526d74e0058d46154e97cbf758edaf3d360e2f3347ae5946ca337eb0d201df8f625e7ae5bfc32e8394d2ce37bd2dc35fa5a4bcecd01@8.131.69.64:32323,cfxnode://c22ad0736f5cc2cc3b11ce5f43345213c2e44994dfaa5e3b0cebe8bd9c78cc52e1a22949ff5953aea80476f648e42b502172e04629c172f4400a0af4caf97efb@8.131.68.192:32323,cfxnode://04cee414977f68a0c2f0215199dde4ec1c27350e447ea855ce000054336f4ccb1c43f0c5ebe8172ad51c7d7b88ac98c037a85ae949e79734449ac38a23fd1d60@106.14.64.36:32323,cfxnode://f1750b818c5828fc5f22667f4b45d4a39b17a1cf40f71ae8f74b6195485a93bf16892a3785bda36132ebae64b83b91b216eaccb7a02185a01f37c7ad89c513ad@101.132.133.254:32323,cfxnode://72a21ec3d2d7c5545b4a46656eaca6ab4ec3ac85628f665bd205e7c52273d345d1583efface277b967fca963a81fbf8b7a81ae97f0a46234cd5fb34853c95fd2@47.101.39.91:32323,cfxnode://b7aeba1f1b2b3e5dfdc7ac93df4281a440ccbdc89894444e094f15242ffa1578d90f9fd447b899be89a57542616e26a82180bd2bfb3b81f82a4dffdfe180f44e@8.210.110.149:32323,cfxnode://07faaf8be8bff4243b496363fb02bd0a21be97e291febcd9aabb29996de90d0a10065f3383beff09f05cb0bdfaa9655d90550c8abcbf97be0658ce6efd8f9b64@47.254.67.249:32323,cfxnode://b77e95cb41cae81dd82a29a07b776549ff37d93954b46214aa32036280c412cefee57350f22d6a2334347b8e0b5040937f8b7a54f5607c345db8a9626e531c0e@47.254.24.245:32323"

# 数据目录
conflux_data_dir = "./sniffer_data"

# 日志配置
log_level = "info"
log_file = "sniffer.log"
```

---

## 6. 数据输出格式

### 6.1 日志输出

运行时在控制台和日志文件中输出：

```
[2026-07-28 12:00:01.123] [INFO] [SNIFFER] NEW_BLOCK_HASH first seen: hash=0xabc...def, ip=39.97.180.246, node_id=abc123...
[2026-07-28 12:00:03.456] [INFO] [SNIFFER] BLOCK_HEADER: hash=0x123...789, height=95000000, difficulty=..., nonce=..., pos_key=Some(...), ip=8.131.69.64
```

### 6.2 JSON Lines 文件输出

文件名：`sniffer_records.jsonl`

每行一条 JSON 记录：

```json
{"block_hash":"0xabc...def","first_peer_ip":"39.97.180.246","first_peer_node_id":"0x04ce1234...1d60","first_seen_at_ms":1753708801123}
```

### 6.3 异常区块特征记录

通过 `NewBlock` 消息记录的区块头信息，可以直接识别异常区块：

- `difficulty` 为 0 或异常低 → PoW 异常
- `pos_public_key` 为 `None` → PoS 异常
- `nonce` 为 0 或固定值 → 可能的 PoW 异常
- `timestamp` 与网络时间偏差过大 → 可能的伪造区块

---

## 7. P2P 协议消息处理总览

### 7.1 窃听节点监听的消息

| 消息 ID | 名称 | 窃听模式处理 |
|---------|------|-------------|
| `NEW_BLOCK_HASHES` | 新区块哈希广播 | 记录首达 IP，返回 |
| `NEW_BLOCK` | 完整区块广播 | 记录首达 IP + 区块头，返回 |

### 7.2 窃听节点必须响应的消息

| 消息 ID | 名称 | 处理方式 | 是否需修改 |
|---------|------|----------|-----------|
| `STATUS_V2` / `STATUS_V3` | 握手协议 | 现有代码自动处理（发送正确的 chain_id 和 genesis_hash） | 否 |
| `HEARTBEAT` | 心跳 | 现有代码更新 heartbeat 时间戳，并可能触发 `start_sync`（已在步骤 9 中短路） | 否（start_sync 已修改） |
| `GET_BLOCK_HEADERS` | 请求区块头 | 现有代码返回空响应（本地无数据） | 否 |
| `GET_BLOCK_BODIES` | 请求区块体 | 现有代码返回空响应 | 否 |
| `GET_BLOCKS` | 请求区块 | 现有代码返回空响应 | 否 |

### 7.3 窃听节点忽略的消息

| 消息 ID | 名称 | 处理方式 |
|---------|------|----------|
| `TRANSACTIONS` | 交易广播 | 窃听模式直接返回 Ok(())（步骤 16） |
| `TRANSACTION_DIGESTS` | 交易摘要 | 窃听模式直接返回 Ok(())（步骤 16） |
| `GET_BLOCK_HASHES` | 请求区块哈希 | 返回空响应 |

### 7.4 窃听节点不主动发送的消息

| 消息 | 原因 |
|------|------|
| `GetBlockHeaders` | `start_sync` 已短路（步骤 9） |
| `GetBlockBodies` | 不需要区块体 |
| `Transactions` | `TX_TIMER` 已跳过（步骤 12） |
| `NewBlockHashes` | `relay_blocks` 在 catch_up_mode=false 时仍可能触发，但窃听节点不会收到需要 relay 的区块 |
| `StatusV2`（广播） | `broadcast_heartbeat` 已跳过 StatusV2（步骤 10） |

注意：`StatusV3` / `StatusV2` 在 `on_peer_connected` 时仍会发送一次（握手必要），这是正确的行为。

---

## 8. 保持连接的协议兼容性

### 8.1 连接建立流程

```
1. TCP 连接建立
    ↓
2. Hello 消息交换（网络层自动处理，包含协议版本协商）
    ↓
3. on_peer_connected 回调
    ↓
4. send_status() 发送 StatusV3（包含 chain_id=1029, genesis_hash, best_epoch=0, terminal_hashes=[]）
    ↓
5. 对端发送 StatusV3/V2 回复
    ↓
6. status.rs:handle() 验证 chain_id 和 genesis_hash
    ↓
7. 将 peer 从 handshaking_peers 移到 peers
    ↓
8. 调用 start_sync()（窃听模式下直接返回）
    ↓
9. 连接建立完成，开始监听消息
```

### 8.2 连接保持条件

| 条件 | 实现方式 | 窃听模式状态 |
|------|----------|-------------|
| 完成 Status 握手 | `send_status()` 在 `on_peer_connected` 中调用 | 正常 |
| 180 秒内收到消息 | 依赖对端发送 Heartbeat / NewBlockHashes | 正常 |
| 主动发送 Heartbeat | `HEARTBEAT_TIMER` 触发 `broadcast_heartbeat` | 正常（只发 Heartbeat，跳过 StatusV2） |
| 不发送格式错误消息 | 使用现有消息编解码 | 正常 |

### 8.3 不会导致断连的行为

| 行为 | 后果 | 风险 |
|------|------|------|
| 不响应 `GET_BLOCK_HEADERS`（返回空） | 对端收到空响应，无超时 | 无 |
| 不请求任何区块 | 对端不会注意到 | 无 |
| best_epoch 始终为 0 | 对端认为我们是新节点，正常 | 无 |

---

## 9. 数据分析方案

### 9.1 基础查询

```bash
# 查看所有记录
cat sniffer_records.jsonl

# 按 IP 统计首达次数
cat sniffer_records.jsonl | jq -r '.first_peer_ip' | sort | uniq -c | sort -rn

# 按时间范围查询
cat sniffer_records.jsonl | jq 'select(.first_seen_at_ms >= 1753708800000 and .first_seen_at_ms < 1753708860000)'

# 查找特定区块哈希
cat sniffer_records.jsonl | jq 'select(.block_hash == "0xabc...def")'
```

### 9.2 异常区块分析

```bash
# 导出所有区块哈希
cat sniffer_records.jsonl | jq -r '.block_hash' > block_hashes.txt

# 查找频繁出块的 IP（可能可疑）
cat sniffer_records.jsonl | jq -r '.first_peer_ip' | sort | uniq -c | sort -rn | head -20
```

### 9.3 与链上数据关联

使用 Conflux RPC 查询区块详情，验证 PoW 和 PoS 信息：

```bash
curl -X POST --data '{
    "jsonrpc":"2.0",
    "method":"cfx_getBlockByHash",
    "params":["0xabc...def", true],
    "id":1
}' -H "Content-Type: application/json" http://main-net-rpc.conflux-chain.org
```

---

## 10. 编译和运行

### 10.1 编译

```bash
cargo build --release --bin conflux
```

### 10.2 运行

```bash
./target/release/conflux --config sniffer.toml
```

### 10.3 监控

```bash
# 查看实时日志
tail -f sniffer.log

# 查看记录文件
tail -f sniffer_records.jsonl

# 统计连接数
grep "peer connected" sniffer.log | wc -l
```

---

## 11. 修改文件清单

| # | 文件路径 | 修改类型 | 行数 |
|---|----------|----------|------|
| 1 | `crates/network/src/lib.rs` | 添加 trait 方法 | ~10 |
| 2 | `crates/network/src/service.rs` | 实现 trait 方法 | ~15 |
| 3 | `crates/cfxcore/core/src/sync/message/handleable.rs` | 扩展 Context | ~5 |
| 4 | `crates/cfxcore/core/src/sync/synchronization_protocol_handler.rs` | 多方法修改（步骤 4,6,7,8,9,10,11,12,15） | ~120 |
| 5 | `crates/cfxcore/core/src/sync/message/new_block_hashes.rs` | 记录逻辑 | ~15 |
| 6 | `crates/cfxcore/core/src/sync/message/new_block.rs` | 记录逻辑 | ~15 |
| 7 | `crates/cfxcore/core/src/sync/message/transactions.rs` | 窃听模式守卫 | ~12 |
| 8 | `crates/cfxcore/core/src/sync/state/snapshot_chunk_sync.rs` | 补充 Context 字段 | ~3 |
| 9 | `crates/config/src/configuration.rs` | 配置项 | ~10 |
| **总计** | | | **~205** |

---

## 12. 关键实现细节

### 12.1 协议版本

```rust
// crates/cfxcore/core/src/sync/mod.rs
const SYNCHRONIZATION_PROTOCOL_VERSION: ProtocolVersion = ProtocolVersion(3);
const SYNCHRONIZATION_PROTOCOL_OLD_VERSIONS_TO_SUPPORT: u8 = 2;
```

窃听节点使用 V3 协议，最低支持 V1。V3 Status 消息包含 `node_type` 字段。

### 12.2 主网 Genesis Block

窃听节点必须使用与主网相同的 genesis block。当 `chain_id = 1029` 时，代码自动使用主网 genesis 配置。`genesis_hash` 通过 `self.graph.data_man.true_genesis.hash()` 获取，用于 Status 握手验证。

### 12.3 PoS 公钥

`SessionManager.self_pos_public_key` 类型为 `Option`，设为 `None` 即可。`on_peer_connected` 的 `_pos_public_key` 参数（对端的 PoS 公钥）在处理器中未使用。窃听节点不需要生成 PoS 密钥对。

### 12.4 最小化存储

- `NetworkConfiguration.config_path` 设为 `None`：禁用 NodeDatabase 持久化
- `bench_mode = true`：跳过交易执行和状态验证
- `DataManagerConfiguration` 最小化缓存配置

### 12.5 窃听节点初始化流程

```
1. 解析配置 (sniffer.toml, sniffer_mode=true)
    ↓
2. 初始化数据库 (StorageManager 仍需初始化以支持 genesis block)
    ↓
3. 创建 Genesis Block (chain_id=1029)
    ↓
4. 初始化 BlockDataManager (极小 cache)
    ↓
5. 初始化 ConsensusGraph (bench_mode=true)
    ↓
6. 初始化 SynchronizationGraph
    ↓
7. 初始化 SynchronizationProtocolHandler (设置 sniffer_mode=true)
    ↓
8. 启动窃听写入线程
    ↓
9. 初始化 NetworkService (config_path=None, self_pos_public_key=None)
    ↓
10. 注册 "cfx" 协议 (V3)
    ↓
11. 启动网络服务，开始监听
```

### 12.6 ConsensusGraph 初始化的必要性

窃听节点虽然不做共识，但 `SynchronizationProtocolHandler` 的多个方法依赖 `self.graph.consensus.best_info()`：
- `produce_status_message_v2()` / `produce_status_message_v3()` 用于 Status 握手
- `produce_heartbeat_message()` 用于心跳广播

因此 `ConsensusGraph` 必须初始化（使用 `bench_mode=true` 跳过交易执行），使其能够返回 genesis 级别的 `best_info()`。

---

## 13. 逻辑完整性检查

### 13.1 消息流入路径检查

| 入站消息 | 处理路径 | 窃听模式是否正确处理 |
|----------|----------|---------------------|
| `STATUS_V3` / `STATUS_V2` | `status.rs:handle()` → 验证 chain_id/genesis_hash → `start_sync()`（已短路） | 是 |
| `HEARTBEAT` | `heartbeat.rs:handle()` → 更新 peer epoch → `start_sync()`（已短路） | 是 |
| `NEW_BLOCK_HASHES` | `new_block_hashes.rs:handle()` → 窃听模式记录后返回 | 是 |
| `NEW_BLOCK` | `new_block.rs:handle()` → 窃听模式记录后返回 | 是 |
| `GET_BLOCK_HEADERS` | `get_block_headers.rs:handle()` → 返回空响应 | 是 |
| `TRANSACTIONS` | `transactions.rs:handle()` → 窃听模式直接返回（步骤 16） | 是 |

**已解决**：`TRANSACTIONS` 消息在 `catch_up_mode()` 返回 `false` 时会进入正常处理路径（第 85-108 行），调用 `tx_pool().insert_new_transactions()` 尝试将交易插入交易池，并调用 `request_manager.append_received_transactions()` 将交易添加到请求管理器。即使 `bench_mode=true`，交易池仍可能尝试验证交易的 nonce、余额等状态依赖信息，导致异常。解决方案见步骤 16，在 `TRANSACTIONS` 和 `TransactionDigests` 处理器中添加窃听模式提前返回。

### 13.2 消息流出路径检查

| 出站消息 | 触发条件 | 窃听模式是否阻止 |
|----------|----------|-----------------|
| `StatusV3` / `StatusV2`（握手） | `on_peer_connected` → `send_status()` | 否（保留，握手必要） |
| `StatusV2`（广播） | `broadcast_heartbeat` | 是（步骤 10 已跳过） |
| `Heartbeat`（广播） | `broadcast_heartbeat` | 否（保留，心跳必要） |
| `GetBlockHeaders` | `start_sync` → `request_missing_terminals` | 是（步骤 9 已短路） |
| `Transactions` | `TX_TIMER` → `propagate_new_transactions` | 是（步骤 12 已跳过） |
| `DynamicCapability` | `update_sync_phase` | 是（步骤 11 已短路） |

### 13.3 定时器检查

| 定时器 | 窃听模式 | 理由 |
|--------|---------|------|
| `TX_TIMER` | 跳过 | 无交易需传播 |
| `CHECK_FUTURE_BLOCK_TIMER` | 跳过 | 无区块需检查 |
| `CHECK_REQUEST_TIMER` | 保留 | 清理过期请求，维持状态一致 |
| `HEARTBEAT_TIMER` | 保留 | 发送心跳，保持连接 |
| `BLOCK_CACHE_GC_TIMER` | 跳过 | 无缓存数据 |
| `CHECK_CATCH_UP_MODE_TIMER` | 跳过 | 不执行阶段转换 |
| `LOG_STATISTIC_TIMER` | 保留 | 日志无害，有助于调试 |
| `TOTAL_WEIGHT_IN_PAST_TIMER` | 跳过 | 无共识数据 |
| `CHECK_PEER_HEARTBEAT_TIMER` | 保留 | 清理死连接 |
| `EXPIRE_BLOCK_GC_TIMER` | 跳过 | 无区块数据 |

### 13.4 潜在风险点

1. **relay_blocks 触发**：`relay_blocks()` 在 `catch_up_mode()` 返回 `false` 时会广播 `NewBlockHashes`。但窃听节点不会将任何区块插入 `ConsensusGraph`，因此 `need_to_relay` 列表始终为空，此路径不会实际触发。

2. **TRANSACTIONS 消息处理**：已在步骤 16 中解决。`TRANSACTIONS` 和 `TransactionDigests` 处理器在窃听模式下直接返回 `Ok(())`，不会触发 `tx_pool().insert_new_transactions()` 或 `request_manager.append_received_transactions()`。

3. **phase_manager 初始状态**：窃听节点启动时 `phase_manager` 处于 `CatchUpRecoverBlockHeaderFromDB` 阶段。虽然 `update_sync_phase` 已短路，但 `phase_manager` 的状态不会改变。如果有其他代码路径直接检查 `phase_type`（而非通过 `catch_up_mode()`），可能需要额外处理。经检查，`start_sync` 已短路，其他直接检查 `phase_type` 的路径（如 `in_recover_from_db_phase()`）在窃听模式下不会被触发（因为 `NewBlock` 和 `NewBlockHashes` 处理已提前返回）。

4. **request_manager.send_pending_requests**：`on_peer_connected` 末尾（第 1855 行附近）调用 `request_manager.send_pending_requests(io, peer)`。由于 `start_sync` 已短路，不会向 request_manager 添加任何请求，因此 `send_pending_requests` 的 pending 队列为空，不会发送任何消息。此路径无需额外修改。

---

## 14. 风险评估

| 风险 | 可能性 | 影响 | 缓解措施 |
|------|--------|------|----------|
| 修改导致正常节点功能异常 | 低 | 中 | 窃听模式通过配置开关控制，默认 false，不影响正常模式 |
| 连接数过多导致资源耗尽 | 中 | 低 | 限制最大连接数（256），监控系统资源 |
| 被其他节点封禁 | 低 | 低 | 不发送垃圾消息，正常响应请求 |
| 编译失败 | 低 | 中 | 修改量约 205 行，在现有代码框架内 |
| TRANSACTIONS 处理异常 | 已解决 | 低 | 步骤 16 在交易处理器中添加窃听模式守卫 |
| 同步 I/O 阻塞 | 中 | 中 | 使用 channel + 专用写入线程 |
| Status 握手失败 | 低 | 高 | 使用与正常节点相同的 Status 响应逻辑 |
| 心跳超时断连 | 低 | 中 | 正常参与心跳交互 |
| 内存泄漏（block_first_seen 无限增长） | 中 | 低 | 定期清理旧记录或使用 LRU 缓存 |

---

## 15. 内存管理补充

### 15.1 block_first_seen 有界去重

`block_first_seen` 使用 `BlockFirstSeenTracker` 进行有界去重，内部维护 `HashSet<H256>` 用于 O(1) 查重和 `VecDeque<H256>` 用于 FIFO 淘汰。当已跟踪的区块哈希数量超过 `SNIFFER_MAX_SEEN_BLOCKS`（默认 50,000）时，最旧的条目会被自动淘汰。

主网每天产生约 300,000 个区块，50,000 条容量约覆盖 4 小时。由于 JSONL 文件已持久化所有首次见到的区块信息，内存中的去重集合仅用于避免对同一区块的重复写入。旧条目被淘汰后，如果同一区块哈希再次出现（极小概率），会重复写入一条记录，但不影响数据分析的正确性。

该设计确保长期运行时内存占用恒定（约 1.6 MB），不会随运行时间增长。

---

## 16. 总结

本计划通过修改 Conflux-Rust 源代码约 205 行，实现一个区块传播窃听节点。核心修改分为四类：

1. **信息采集**（步骤 1-4, 13-14, 16）：扩展 `NetworkContext` trait 和 `Context` 结构体以获取 peer IP，在 `NewBlockHashes` 和 `NewBlock` 处理中记录首达信息，在 `TRANSACTIONS` 处理中添加窃听模式守卫
2. **请求抑制**（步骤 9, 10, 11, 12, 15）：短路 `start_sync`、跳过 StatusV2 广播、跳过阶段转换、跳过不必要的定时器、跳过工作分发
3. **状态伪装**（步骤 8）：让 `catch_up_mode()` 返回 `false`，使消息处理进入正常路径但实际不做任何同步工作
4. **数据持久化**（步骤 6-7）：使用 channel + 专用线程异步写入 JSON Lines 文件

该方案与 Conflux P2P 协议完全兼容：正确完成 Status 握手、正常发送 Heartbeat、正常响应请求（返回空数据）、不主动发起任何同步请求，不会导致连接被断开，也不会给其他节点制造性能压力。
