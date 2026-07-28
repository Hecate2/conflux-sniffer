# conflux-sniffer

基于 Conflux-Rust 的主网区块传播窃听节点。通过被动监听 P2P 网络中的 `NewBlockHashes` 和 `NewBlock` 消息，记录每个区块的哈希值以及最先将该区块传播给本节点的 peer IP 地址和 NodeId。

---

## 1. 快速开始

### 1.1 编译

项目基于 Conflux-Rust v3.0.3-fix，使用 Rust 编译。在 macOS 上需要额外指定 bzip2 库路径（Homebrew 安装的 bzip2 不在标准搜索路径中）：

```bash
# macOS (Apple Silicon, Homebrew)
CMAKE_MAKE_PROGRAM=/usr/bin/make \
RUSTFLAGS="-L /opt/homebrew/lib" \
LIBRARY_PATH=/opt/homebrew/lib \
cargo build --release --bin conflux

# Linux
cargo build --release --bin conflux
```

首次编译约需 10-15 分钟（依赖 RocksDB、librocksdb_sys 等原生库），后续增量编译约 30 秒。

### 1.2 配置

配置文件位于 `run/sniffer.toml`，核心配置项：

```toml
# 窃听模式开关（核心配置）
sniffer_mode = true
sniffer_log_file = "sniffer_records.jsonl"

# 主网参数
chain_id = 1029
evm_chain_id = 1030
hydra_transition_number = 92060600
hydra_transition_height = 36935000
cip43_init_end_number = 92751800
pos_reference_enable_height = 37400000

# 最大化连接数
max_incoming_peers = 200
max_outgoing_peers = 200
max_handshakes = 128

# 放宽 IP 限制以连接更多节点
session_ip_limits = "0,0,0,0"
subnet_quota = 0

# 数据目录（独立于正常节点）
conflux_data_dir = "./sniffer_data"

# 禁用挖矿和交易传播
mining_type = "disable"
min_peers_tx_propagation = 0
max_peers_tx_propagation = 0

# 不参与 PoS 投票
pos_started_as_voter = false
```

`bootnodes` 字段已预填 Conflux 主网官方 bootnode 列表（26 个节点），节点启动后会通过 Discovery 协议自动发现更多 peer。

### 1.3 运行

```bash
cd run

# 设置 PoS 密钥密码（避免交互式密码提示导致启动失败）
export CFX_POS_KEY_ENCRYPTION_PASSWORD=dummypassword

# 启动窃听节点
../target/release/conflux --config sniffer.toml
```

节点启动后会自动连接主网 peer 并开始记录区块传播数据。运行日志输出到 stdout（同时由 log4rs 写入日志文件），窃听记录写入 `sniffer_records.jsonl`。

### 1.4 限时运行（验证用）

如果只需要短暂运行测试，可以用后台进程加 sleep 的方式限时执行：

```bash
cd run
export CFX_POS_KEY_ENCRYPTION_PASSWORD=dummypassword

# 后台运行 60 秒后自动终止
../target/release/conflux --config sniffer.toml > sniffer_output.log 2>&1 &
SNIFFER_PID=$!
sleep 60
kill $SNIFFER_PID
```

### 1.5 停止

使用 `Ctrl+C` 或 `kill <PID>` 发送 SIGTERM 信号即可正常停止。窃听记录是追加写入的（append 模式），停止后数据不会丢失。

---

## 2. 输出内容

### 2.1 JSON Lines 文件：`sniffer_records.jsonl`

每收到一个之前未见过的区块哈希（通过 `NewBlockHashes` 或 `NewBlock` 消息），就追加写入一行 JSON 记录。文件路径由配置项 `sniffer_log_file` 指定，默认为 `sniffer_records.jsonl`（相对于工作目录）。

每行记录格式：

```json
{
  "block_hash": "0xa4a094a69acb6372fd0c8d33e30db51ccdf77b5a451f01496db863bf87feb003",
  "first_peer_ip": "106.14.64.36",
  "first_peer_node_id": "0x04ce…1d60",
  "first_seen_at": 1785248027
}
```

字段说明：

| 字段 | 类型 | 说明 |
|------|------|------|
| `block_hash` | string | 区块哈希，十六进制 0x 前缀格式 |
| `first_peer_ip` | string 或 null | 最先发送该区块哈希的 peer 的 IP 地址。如果 IP 不可获取则为 null |
| `first_peer_node_id` | string | 最先发送该区块哈希的 peer 的 NodeId（Conflux P2P 节点标识符） |
| `first_seen_at` | integer | 本节点首次收到该区块哈希的 Unix 时间戳（秒级） |

写入机制采用异步线程：网络 IO 线程通过 `std::sync::mpsc::channel` 将记录发送到专用写入线程，由写入线程执行文件 I/O。这确保高频消息场景下不会阻塞网络处理。

每个区块哈希只记录一次（首次收到时）。后续从其他 peer 收到相同哈希时不会重复记录。

### 2.2 运行日志

窃听节点运行时在 stdout 和日志文件中输出标准 Conflux 日志，其中与窃听相关的关键日志行以 `[SNIFFER]` 前缀标识：

```
2026-07-28T22:14:01.184270+08:00 INFO  Socket IO Worker #2  cfxcore::syn - [SNIFFER] NEW_BLOCK_HASH first seen: hash=0x327bc7c06ba0007a47e373032f5f5323d5962108a38037729a348c9c69027301, ip=Some(18.158.251.2), node_id=0x838e…7cd8
```

该日志在每次首次收到新区块哈希时输出，包含区块哈希、来源 IP 和 NodeId。日志级别为 INFO，通过标准 log4rs 配置控制。

其他有用的日志行（非 `[SNIFFER]` 前缀）：

```
# peer 连接状态
WARN  IO Worker #0  network::ser - No peers connected at this moment, 0 pending + 23 started

# 统计信息（定期输出）
INFO  IO Worker #0  cfxcore::sta - Statistics: StatisticsInner { sync_graph: SyncGraphStatistics { inserted_block_count: 1, inserted_header_count: 1 }, ... }
```

### 2.3 数据目录

`conflux_data_dir` 指定的目录（默认 `./sniffer_data`）下会创建以下子目录，用于存储节点运行所需的最小化数据：

| 路径 | 说明 |
|------|------|
| `sniffer_data/blockchain_db/` | RocksDB 区块链数据库（仅存储 genesis block） |
| `sniffer_data/net_config/` | 网络配置和节点表（peer 发现结果持久化） |
| `sniffer_data/storage_db/` | 状态存储数据库（仅初始化，无实际状态数据） |

这些目录可以安全删除以重置节点状态。删除后重启节点会重新初始化并重新发现 peer。

### 2.4 实际运行示例

以下是一次 60 秒试运行的实际输出统计：

| 指标 | 数值 |
|------|------|
| 连接 peer 数 | 23 |
| 捕获区块哈希数 | 127 |
| 不同来源 IP 数 | 20 |

区块来源 IP 分布（前 5）：

| IP | 区块数 | 占比 |
|----|--------|------|
| 18.158.251.2 (AWS Sydney) | 41 | 32% |
| 142.132.250.163 | 13 | 10% |
| 49.13.161.201 | 10 | 8% |
| 195.160.222.82 | 9 | 7% |
| 18.163.95.162 | 8 | 6% |

---

## 3. 数据分析

### 3.1 基础查询

```bash
# 查看所有记录
cat sniffer_records.jsonl

# 按 IP 统计首达次数（找出哪些节点最先传播区块）
cat sniffer_records.jsonl | python3 -c "
import json, sys
from collections import Counter
ips = Counter()
for line in sys.stdin:
    r = json.loads(line)
    ips[r.get('first_peer_ip', 'None')] += 1
for ip, count in ips.most_common():
    print(f'  {ip}: {count} blocks')
print(f'Total unique IPs: {len(ips)}')
"

# 按时间范围查询
cat sniffer_records.jsonl | python3 -c "
import json, sys
for line in sys.stdin:
    r = json.loads(line)
    if r['first_seen_at'] >= 1785248027 and r['first_seen_at'] < 1785248087:
        print(json.dumps(r))
"

# 查找特定区块哈希
cat sniffer_records.jsonl | grep "0xa4a094a6"
```

### 3.2 异常区块调查流程

1. **收集数据**：运行窃听节点足够长时间（建议至少 1 小时），收集 `sniffer_records.jsonl`

2. **识别可疑 IP**：统计哪些 IP 最频繁地最先传播区块。正常情况下，区块应该由不同的矿工节点或 relay 节点传播。如果某个 IP 异常频繁地最先传播区块，可能是可疑区块的来源

3. **关联链上数据**：将 `sniffer_records.jsonl` 中的 `block_hash` 与 Conflux 链上数据交叉验证：

```bash
# 查询区块详情（通过公共 RPC）
curl -X POST --data '{
    "jsonrpc":"2.0",
    "method":"cfx_getBlockByHash",
    "params":["0xa4a094a69acb6372fd0c8d33e30db51ccdf77b5a451f01496db863bf87feb003", true],
    "id":1
}' -H "Content-Type: application/json" https://main.confluxrpc.com
```

4. **检查异常特征**：通过 RPC 返回的区块详情检查以下异常指标：
   - `difficulty` 为 0 或异常低
   - `powQuality` 不满足 difficulty 要求
   - `posPublicKey` 为 null（PoS 未签名）
   - `nonce` 为固定值
   - `timestamp` 与网络时间偏差过大

---

## 4. 方案选择：直接修改 Conflux-Rust

### 4.1 为什么不能另起仓库

用户最初考虑另起仓库调用 Conflux crates。经过对源代码的详细分析，确认此方案不可行，原因如下：

**问题一：无法绕过 cfxcore 的 God Crate 问题**

`SynchronizationProtocolHandler` 是 cfxcore 的核心类型，其结构体持有 `SharedSynchronizationGraph`、`Arc<RequestManager>`、`SynchronizationState`、`SynchronizationPhaseManager`、`LightProvider` 等字段的直接引用。这些类型之间形成了紧密的相互引用关系。例如 `produce_status_message_v2()` 调用 `self.graph.consensus.best_info()`，`broadcast_heartbeat()` 同时调用 `produce_status_message_v2()` 和 `produce_heartbeat_message()`，两者都依赖 `ConsensusGraph` 的内部状态。要将 `SynchronizationProtocolHandler` 单独提取到新仓库，必须同时搬移 ConsensusGraph、BlockDataManager、RequestManager、SynchronizationState 等全部依赖链，这等同于搬移整个 cfxcore。

**问题二：NetworkProtocolHandler trait 的方法签名依赖**

`on_message` 接收的 `data: &[u8]` 需要调用 `decode_msg()`（定义在 `crates/cfxcore/core/src/message.rs`）才能解析出 `MsgId` 和 `Rlp`，而所有消息类型（`StatusV2`、`StatusV3`、`NewBlockHashes`、`NewBlock`、`Heartbeat` 等）全部定义在 cfxcore 中。在新仓库中实现 `on_message` 意味着要么重新实现所有消息类型的 RLP 编解码，要么仍然依赖 cfxcore。

**问题三：Context 结构体的硬依赖**

所有消息处理器都实现 `Handleable` trait，其 `handle()` 方法签名接收 `&Context`，而 `Context` 持有 `&SynchronizationProtocolHandler`。任何想要复用消息处理逻辑的代码都必须依赖 `SynchronizationProtocolHandler`。

**结论**：另起仓库无法避免对 cfxcore 的深度依赖。直接修改 Conflux-Rust 源代码只需添加约 200 行修改，且能复用全部基础设施。

### 4.2 直接修改的优势

通过 `sniffer_mode` 配置标志做条件分支，不改变原有逻辑路径。修改集中在少数文件中，合并上游更新时冲突极少。使用与正常节点完全相同的 Status 握手、Heartbeat 机制，协议兼容性有保障。

---

## 5. 逻辑漏洞分析与解决方案

以下分析描述了实现过程中识别并修复的关键逻辑漏洞。每一条都对应实际代码中的 sniffer_mode 守卫。

### 5.1 catch_up_mode 下 NewBlockHash 被忽略

新节点启动时处于 `CatchUpRecoverBlockHeaderFromDB` 阶段，`catch_up_mode()` 返回 `true`，导致 `NewBlockHashes::handle()` 中的 `catch_up_mode()` 检查会将消息直接丢弃。

**解决方案**：在 `NewBlockHashes::handle()` 开头添加 `sniffer_mode` 检查，无条件记录后直接返回，绕过 `catch_up_mode()` 检查。

### 5.2 NewBlock 处理会触发额外请求

处理 `NewBlock` 消息会调用 `request_block_headers()`，向对端请求父区块头和引用区块头，给其他节点制造不必要的流量。

**解决方案**：在 `NewBlock::handle()` 开头添加 `sniffer_mode` 检查，只记录区块哈希和来源 IP，不触发任何请求。

### 5.3 start_sync 向所有 peer 发送 GetBlockHeaders 请求

`start_sync()` 在握手完成后和收到 Status/Heartbeat 消息时被调用。即使 `catch_up_mode()` 返回 `false`，`start_sync()` 的 `else` 分支仍会调用 `request_missing_terminals()`，对每个 peer 发送 `GetBlockHeaders` 请求。`start_sync()` 内部检查的是 `phase_type` 而非 `catch_up_mode()`，因此仅修改 `catch_up_mode()` 的返回值不足以阻止此行为。

**解决方案**：在 `start_sync()` 开头添加 `sniffer_mode` 检查直接返回。

### 5.4 broadcast_heartbeat 广播不必要的 StatusV2

`broadcast_heartbeat()` 同时广播 `Heartbeat` 和 `StatusV2` 消息。窃听节点未同步任何区块，`best_epoch` 为 0，广播 StatusV2 是不必要的流量，且可能触发对端不必要的同步帮助行为。

**解决方案**：在 `broadcast_heartbeat()` 中对 StatusV2 广播添加 `sniffer_mode` 守卫，仅保留 Heartbeat 广播。注意 `on_peer_connected` 中的 `send_status()` 必须保留，因为 Status 握手是建立连接的必要条件。

### 5.5 update_sync_phase 触发阶段转换和能力广播

`update_sync_phase()` 由 `CHECK_CATCH_UP_MODE_TIMER` 定时器触发，会尝试阶段转换并向所有 peer 广播 `DynamicCapability::NormalPhase`。窃听节点不需要阶段转换。

**解决方案**：在 `update_sync_phase()` 开头添加 `sniffer_mode` 检查直接返回。

### 5.6 on_timeout 中多个定时器执行不必要的工作

| 定时器 | 行为 | 窃听模式处理 |
|--------|------|-------------|
| `TX_TIMER` | 传播新交易 | 跳过 |
| `CHECK_FUTURE_BLOCK_TIMER` | 检查未来区块 + 调用 consensus graph | 跳过 |
| `CHECK_REQUEST_TIMER` | 清理过期请求 | 保留（维持状态一致） |
| `HEARTBEAT_TIMER` | 发送心跳 | 保留（保持连接活跃） |
| `BLOCK_CACHE_GC_TIMER` | 缓存垃圾回收 | 跳过 |
| `CHECK_CATCH_UP_MODE_TIMER` | 调用 update_sync_phase | 跳过 |
| `LOG_STATISTIC_TIMER` | 日志统计 | 保留（有助于调试） |
| `TOTAL_WEIGHT_IN_PAST_TIMER` | 更新共识权重 | 跳过 |
| `CHECK_PEER_HEARTBEAT_TIMER` | 检查对端心跳超时并断连 | 保留（清理死连接） |
| `EXPIRE_BLOCK_GC_TIMER` | 过期区块清理 | 跳过 |

### 5.7 catch_up_mode 判断逻辑

`catch_up_mode()` 的返回值被 `relay_blocks`、`propagate_new_transactions` 等代码路径使用。在窃听模式下统一返回 `false` 以避免这些路径的异常行为。仅修改 `catch_up_mode()` 不足以阻止 `start_sync()` 发送请求（见 5.3），两者必须同时修改。

### 5.8 同步文件 I/O 阻塞 IO 线程

在高消息率场景下（主网每秒可能收到数十条 NewBlockHash 消息），同步文件 I/O 会阻塞消息处理线程。

**解决方案**：使用 `std::sync::mpsc::channel` 将记录发送到专用写入线程，IO 线程只做非阻塞的 channel send。写入线程负责打开文件和执行 `writeln!`。

### 5.9 TRANSACTIONS 消息处理异常

`TRANSACTIONS` 消息处理器在 `catch_up_mode()` 返回 `false` 时会调用 `tx_pool().insert_new_transactions()` 将交易插入交易池，触发 nonce、余额等状态验证。窃听节点的状态不正确，可能导致 panic。

**解决方案**：在 `Transactions::handle()` 和 `TransactionDigests::handle()` 开头添加 `sniffer_mode` 检查直接返回 `Ok(())`。

### 5.10 on_work_dispatch

`on_work_dispatch` 处理区块恢复等工作分发。窃听模式不需要处理任何工作分发。

**解决方案**：在 `on_work_dispatch()` 开头添加 `sniffer_mode` 检查直接返回。

### 5.11 GET_BLOCK_HEADERS 等请求的响应

其他节点可能向窃听节点请求区块头、区块体等数据。现有代码通过 `graph.data_man.block_header_by_hash()` 查询本地数据，查询结果为空时自然返回空响应。窃听节点本地无区块数据，所有查询都返回空，不会给对端造成额外压力。此路径无需修改。

### 5.12 Status 握手兼容性

窃听节点使用与正常节点完全相同的 Status 响应逻辑。`on_peer_connected` 调用 `send_status()`，后者通过 `produce_status_message_v3()` 或 `produce_status_message_v2()` 构造 Status 消息，其中 `chain_id` 来自 `best_info.best_chain_id()`（在正确初始化 genesis block 后即为 1029），`genesis_hash` 来自 `self.graph.data_man.true_genesis.hash()`。此路径无需修改。

### 5.13 心跳超时断连

Conflux 使用心跳机制检测连接活跃性，默认超时时间 180 秒。窃听节点正常参与心跳交互：`HEARTBEAT_TIMER` 定时器触发 `broadcast_heartbeat()` 向所有 peer 发送 Heartbeat 消息，`CHECK_PEER_HEARTBEAT_TIMER` 定时器检查对端心跳超时并清理死连接。只要双方在 180 秒内有消息交互（包括 Heartbeat、NewBlockHashes 等），连接就不会断开。

### 5.14 PoS 公钥问题

`SessionManager` 的 `self_pos_public_key` 字段类型为 `Option`，可以设为 `None`。`on_peer_connected` 的参数 `_pos_public_key` 带下划线前缀，表示对端的 PoS 公钥在当前处理器中未使用。窃听节点不需要生成自己的 PoS 密钥对。运行时需要设置 `CFX_POS_KEY_ENCRYPTION_PASSWORD` 环境变量以跳过交互式密码提示。

---

## 6. 实际修改的文件清单

以下列出实际已修改的文件及其修改内容：

| 文件路径 | 修改内容 |
|----------|----------|
| `crates/network/src/lib.rs` | 在 `NetworkContext` trait 中添加 `get_peer_addr()` 方法声明 |
| `crates/network/src/service.rs` | 实现 `get_peer_addr()`，通过 `sessions.get_by_id()` 获取 peer 的 `SocketAddr` |
| `crates/cfxcore/core/src/sync/message/handleable.rs` | 在 `Context` 结构体中添加 `peer_addr: Option<SocketAddr>` 字段 |
| `crates/cfxcore/core/src/sync/synchronization_protocol_handler.rs` | 核心修改：添加 `BlockFirstSeen` 结构体、`block_first_seen` 字段、`sniffer_writer_tx` 字段、`start_sniffer_writer()` 异步写入线程、`record_block_hash_first_seen()` 记录方法；在 `catch_up_mode()`、`start_sync()`、`broadcast_heartbeat()`、`update_sync_phase()`、`on_work_dispatch()`、`on_timeout()` 中添加 sniffer_mode 守卫；在 `dispatch_message()` 中填充 `peer_addr` |
| `crates/cfxcore/core/src/sync/message/new_block_hashes.rs` | 在 `handle()` 开头添加 sniffer_mode 检查，记录首达 IP 后返回 |
| `crates/cfxcore/core/src/sync/message/new_block.rs` | 在 `handle()` 开头添加 sniffer_mode 检查，记录首达 IP 后返回 |
| `crates/cfxcore/core/src/sync/message/transactions.rs` | 在 `Transactions::handle()` 和 `TransactionDigests::handle()` 开头添加 sniffer_mode 检查直接返回 |
| `crates/cfxcore/core/src/sync/state/snapshot_chunk_sync.rs` | 在 `Context` 构造点补充 `peer_addr: None` 字段 |
| `crates/config/src/configuration.rs` | 在 `build_config!` 宏中添加 `sniffer_mode` 和 `sniffer_log_file` 配置项；在 `ProtocolConfiguration` 中添加 `sniffer_mode` 字段 |
| `run/sniffer.toml` | 新增窃听节点配置文件 |
| `.gitignore` | 添加 `pos_key`、`sniffer_data/`、`sniffer_records.jsonl`、`pos_config/` 的忽略规则 |

---

## 7. P2P 协议消息处理总览

### 7.1 窃听节点监听并记录的消息

| 消息 | 窃听模式处理 |
|------|-------------|
| `NEW_BLOCK_HASHES` | 记录每个区块哈希的首达 IP 和 NodeId，写入 `sniffer_records.jsonl`，返回 `Ok(())` |
| `NEW_BLOCK` | 记录区块哈希的首达 IP 和 NodeId，写入 `sniffer_records.jsonl`，返回 `Ok(())` |

### 7.2 窃听节点正常响应的消息

| 消息 | 处理方式 | 是否需修改 |
|------|----------|-----------|
| `STATUS_V2` / `STATUS_V3` | 现有代码自动处理（发送正确的 chain_id 和 genesis_hash） | 否 |
| `HEARTBEAT` | 现有代码更新 heartbeat 时间戳，触发 `start_sync()`（已短路） | 否（start_sync 已修改） |
| `GET_BLOCK_HEADERS` | 现有代码返回空响应（本地无数据） | 否 |
| `GET_BLOCK_BODIES` | 现有代码返回空响应 | 否 |
| `GET_BLOCKS` | 现有代码返回空响应 | 否 |

### 7.3 窃听节点忽略的消息

| 消息 | 处理方式 |
|------|----------|
| `TRANSACTIONS` | sniffer_mode 检查直接返回 `Ok(())` |
| `TRANSACTION_DIGESTS` | sniffer_mode 检查直接返回 `Ok(())` |

### 7.4 窃听节点不主动发送的消息

| 消息 | 阻止方式 |
|------|----------|
| `GetBlockHeaders` | `start_sync()` 已短路 |
| `GetBlockBodies` | 不需要区块体 |
| `Transactions` | `TX_TIMER` 已跳过 |
| `StatusV2`（广播） | `broadcast_heartbeat()` 中已跳过 StatusV2 |

注意：`StatusV3`/`StatusV2` 在 `on_peer_connected` 时仍会发送一次（握手必要），这是正确的行为。

---

## 8. 保持连接的协议兼容性

### 8.1 连接建立流程

```
1. TCP 连接建立
2. Hello 消息交换（网络层自动处理，包含协议版本协商）
3. on_peer_connected 回调
4. send_status() 发送 StatusV3（包含 chain_id=1029, genesis_hash, best_epoch=0, terminal_hashes=[]）
5. 对端发送 StatusV3/V2 回复
6. status.rs:handle() 验证 chain_id 和 genesis_hash
7. 将 peer 从 handshaking_peers 移到 peers
8. 调用 start_sync()（窃听模式下直接返回）
9. 连接建立完成，开始监听消息
```

### 8.2 连接保持条件

| 条件 | 实现方式 | 窃听模式状态 |
|------|----------|-------------|
| 完成 Status 握手 | `send_status()` 在 `on_peer_connected` 中调用 | 正常 |
| 180 秒内收到消息 | 依赖对端发送 Heartbeat / NewBlockHashes | 正常 |
| 主动发送 Heartbeat | `HEARTBEAT_TIMER` 触发 `broadcast_heartbeat` | 正常（只发 Heartbeat，跳过 StatusV2） |
| 不发送格式错误消息 | 使用现有消息编解码 | 正常 |

---

## 9. 关键实现细节

### 9.1 配置项访问路径

sniffer_mode 标志存储在 `ProtocolConfiguration` 中，通过 `self.protocol_config.sniffer_mode` 访问。在 `SynchronizationProtocolHandler::new()` 中，`sniffer_mode` 的值在 `protocol_config` 被 move 之前提前提取，用于决定是否初始化写入线程：

```rust
let sniffer_mode = protocol_config.sniffer_mode;
Self {
    protocol_config,
    // ...
    sniffer_writer_tx: if sniffer_mode {
        Some(Self::start_sniffer_writer("sniffer_records.jsonl"))
    } else {
        None
    },
}
```

### 9.2 BlockFirstSeen 数据结构

```rust
pub struct BlockFirstSeen {
    pub block_hash: H256,
    pub first_peer_ip: Option<std::net::IpAddr>,
    pub first_seen_at: SystemTime,
    pub first_peer_node_id: NodeId,
}
```

使用 `SystemTime` 而非 `Instant`，因为需要将时间戳转换为 Unix epoch 秒数写入 JSONL 文件。`Instant` 只能用于测量时间间隔，不能转换为绝对时间。

### 9.3 异步写入线程

`start_sniffer_writer()` 创建一个 `std::sync::mpsc::channel` 和一个专用写入线程。写入线程以 append 模式打开 JSONL 文件，循环从 channel 接收 `BlockFirstSeen` 记录并写入文件。网络 IO 线程通过 `tx.send(entry)` 非阻塞地将记录发送到写入线程。

### 9.4 主网 Genesis Block

当 `chain_id = 1029` 时，代码自动使用主网 genesis 配置。`genesis_hash` 通过 `self.graph.data_man.true_genesis.hash()` 获取，用于 Status 握手验证。窃听节点必须使用与主网相同的 genesis block 才能通过握手。

### 9.5 ConsensusGraph 初始化的必要性

窃听节点虽然不做共识，但 `SynchronizationProtocolHandler` 的多个方法依赖 `self.graph.consensus.best_info()`，包括 `produce_status_message_v2()`/`produce_status_message_v3()`（Status 握手）和 `produce_heartbeat_message()`（心跳广播）。因此 `ConsensusGraph` 必须初始化，使其能够返回 genesis 级别的 `best_info()`。

### 9.6 内存管理

`block_first_seen: Arc<RwLock<HashMap<H256, BlockFirstSeen>>>` 会随时间持续增长。主网每天产生约 300,000 个区块，长期运行会消耗大量内存。当前实现尚未添加容量限制。如果需要长期运行，建议定期重启节点，或在 `record_block_hash_first_seen()` 中添加 LRU 淘汰逻辑。

---

## 10. 已知限制与未来改进

| 项目 | 当前状态 | 改进方向 |
|------|----------|----------|
| 区块头记录 | 仅记录区块哈希和来源 IP，未记录 difficulty/nonce/pos_public_key 等区块头字段 | 在 `NewBlock::handle()` 中提取并记录完整区块头信息，便于直接识别异常区块 |
| 内存管理 | `block_first_seen` HashMap 无限增长 | 添加容量限制（如 100,000 条），超过时淘汰最旧记录 |
| 时间戳精度 | `first_seen_at` 为秒级 | 改为毫秒级以支持更精细的传播延迟分析 |
| 多消息类型记录 | `NewBlockHashes` 和 `NewBlock` 共用同一个记录函数，无法区分来源消息类型 | 在 `BlockFirstSeen` 中添加 `source_msg_type` 字段 |
| 配置灵活性 | `sniffer_log_file` 配置项已定义但实际硬编码为 `"sniffer_records.jsonl"` | 在 `start_sniffer_writer()` 中使用配置值 |
