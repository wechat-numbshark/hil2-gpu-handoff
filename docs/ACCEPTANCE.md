# 验收清单

交接文件第 11 节的 12 项验收标准，逐条对应到自动化测试。
运行 `make test` 全部通过即视为自动化部分达标；现场验收还需要人工走一遍飞书链路。

```bash
.venv/bin/python -m pytest tests/test_acceptance.py -v
```

| # | 验收标准 | 对应测试 | 状态 |
|---|---|---|---|
| 1 | pytest 运行 → `BUSY`，未到交接点不打扰大群 | `Test01_PytestRunningIsBusyAndQuiet` | ✅ |
| 2 | 无 pytest 但 `/data1` 近期写入 → `BUSY` | `Test02_RecentDataWriteIsBusy` | ✅ |
| 3 | GPU 高、连续 15 分钟无 pytest/写入 → `SUSPECTED_IDLE`，给当前占用人发卡片 | `Test03_HighGpuNoActivityAsksCurrentUser` | ✅ |
| 4 | 核心容器全部停止、无保留 → `IDLE`，直接 `@下一位` | `Test04_AllContainersStoppedNotifiesNext` | ✅ |
| 5 | 用户选择继续 30/60 分钟 → 预约延期，旧卡片不能重复操作 | `Test05_ContinueExtendsAndOldCardDies` | ✅ |
| 6 | 用户选择已使用完 → 预约释放并通知下一位，但不操作机器 | `Test06_ReleaseNotifiesNextWithoutTouchingMachine` | ✅ |
| 7 | 10/20 分钟未回复 → 提醒/升级管理员，不自动释放 | `Test07_TimeoutRemindsThenEscalatesNeverReleases` | ✅ |
| 8 | Docker 权限不足、`/data1` 不可读 → `UNKNOWN`，不宣告可用 | `Test08_CollectionFailureNeverDeclaresAvailable` | ✅ |
| 9 | 监控群持续有心跳 → 业务大群不刷屏 | `Test09_MonitorHeartbeatDoesNotSpamBusinessChat` | ✅ |
| 10 | 非当前占用人或过期卡片点击 → 拒绝 | `Test10_StaleOrUnauthorizedClickRejected` | ✅ |
| 11 | 机器人不会循环消费自己发出的消息 | `Test11_BotDoesNotConsumeItsOwnMessages` | ✅ |
| 12 | Bot-to-Bot 消息无法投递时，直接 HTTP 事件通道仍可工作 | `Test12_DirectChannelWorksWhenBotToBotFails` | ✅ |

## 预约验收（v1.1 新增）

| # | 验收标准 | 对应测试 |
|---|---|---|
| B1 | 时段按半小时对齐，写 14:10 归到 14:00 | `test_booking.py::TestAlignment` |
| B2 | 一台集群同一时间只能一人，冲突被拒并说明被谁占 | `TestConflicts` · `TestBookingApi::test_conflict_returns_409_with_readable_reason` |
| B3 | 相邻时段（9-11 与 11-13）允许，不算冲突 | `TestConflicts::test_touching_edges_do_not_conflict` |
| B4 | 中文口语时间可解析，看不懂则拒绝而非猜测 | `TestTimeParsing` |
| B5 | 只能取消自己的预约，管理员可代取消 | `TestStoreBookings` |
| B6 | 取消后时段立即可被他人预约 | `test_cancelled_slot_becomes_bookable` |
| B7 | 续期不挤占下一位，只延到对方开始为止并说明 | `TestExtension::test_never_squeezes_out_the_next_person` |
| B8 | 交接时刻整点，接班人不会同时被算成「下一位」 | `TestEngineSeam::test_booking_end_triggers_handoff_prompt` |
| B9 | 预约驱动判定：空闲时通知的是预约里的下一位 | `test_booking_drives_handoff_decision` |
| B10 | 大盘卡片的预约信息与预约看板一致 | `test_dashboard_reflects_booking` |
| B11 | 飞书群指令重复投递不会重复下单 | `test_duplicate_message_not_double_booked` |
| B12 | 预约即将开始时提醒一次，不重复 | `TestReminders::test_reminds_once_before_start` |

## 管理后台与双采集模式验收（v1.2 新增）

| # | 验收标准 | 对应测试 |
|---|---|---|
| A1 | 集群配置校验：编号、路径、阈值、正则全部拦得住错误输入 | `test_registry.py::TestValidation` |
| A2 | 部分更新保留未提交字段，不会把没填的项清空 | `TestFromDict::test_partial_update_keeps_existing` |
| A3 | Webhook 含密钥，接口永不回显 | `TestSerialisation::test_webhook_never_echoed_by_default` |
| A4 | 移出纳管保留历史事件与预约 | `TestStorePersistence::test_delete_keeps_history` |
| A5 | SSH 模式远端强制只读只算，无从对外发消息 | `test_collector_ssh.py::TestRemoteCommand::test_remote_cannot_send_anything` |
| A6 | 正则和路径经 shell 引用，特殊字符不被吃掉 | `test_special_chars_are_quoted` |
| A7 | **任何 SSH 失败形态都产出 UNKNOWN，绝不是空闲** | `TestFailClosed::test_no_failure_ever_yields_idle` |
| A8 | 两种模式跑同一份脚本，判定逻辑不会漂移 | `TestSharedLogicWithAgent` |
| A9 | 拉取按每台自己的间隔到点，互不干扰 | `test_puller.py::TestScheduling` |
| A10 | 疑似空闲连击由协调服务记账，采集失败会归零 | `TestStreak::test_failure_resets_streak_not_escalates` |
| A11 | 单台采集异常不拖垮整轮 | `test_one_bad_cluster_does_not_block_others` |
| A12 | 通讯录同步保留本地管理员标记，且不删离职的人 | `test_orgsync.py::TestSync` |
| A13 | 缺企业信息权限时降级提示，不阻断同步 | `test_tenant_permission_missing_degrades` |
| A14 | 安装包配置正确、权限 600、默认灰度 | `test_admin_api.py::TestAgentBundle` |
| A15 | 安全类配置不允许网页修改 | `TestSettings::test_security_settings_rejected` |
| A16 | 后台设置改完立即生效，不用重启 | `test_takes_effect_without_restart` |
| A17 | SSO 下后台拒绝非管理员 | `TestAuthorisation` |
| A18 | 不会把最后一个管理员摘掉导致锁死 | `test_cannot_remove_last_admin_without_env_fallback` |
| A19 | **大盘免登可看，预约和后台需登录** | `test_dashboard_stays_viewable_when_sso_enabled` |
| A20 | 伪造 open_id 无法冒名预约 | `test_writing_still_requires_login_when_sso_enabled` |

## 部署脚本验收

| # | 验收标准 | 对应测试 |
|---|---|---|
| D1 | 非 root 执行干净拒绝 | `test_install_script.sh` 步骤 2 |
| D2 | 生成的 .env 权限 600，密钥不外泄 | 步骤 4 |
| D3 | 默认灰度，装完不发任何消息 | 步骤 4 |
| D4 | 重复执行幂等，旧配置自动备份，地址密钥保留 | 步骤 5 |
| D5 | 生成的 .env 能被安全 source（正则里的 `\|` 不报错） | 步骤 5b |
| D6 | 指定不存在的账号时拒绝，不悄悄换人 | 步骤 6 |

## 飞书长连接入站验收

| # | 验收标准 | 对应测试 |
|---|---|---|
| L1 | 消息事件按 SDK 1.4 的真实嵌套结构保留群、发送人和事件 ID | `test_message_envelope_prefers_body_text_and_preserves_identity` · `test_message_envelope_matches_real_sdk_nested_identity_and_conversation` |
| L2 | 卡片点击在 SDK 未提供 event_id 时仍有稳定幂等键 | `test_card_envelope_has_stable_fallback_event_id` |
| L3 | 长连接同时注册消息、卡片和错误处理，共用业务分发入口 | `test_long_connection_registers_both_handlers_and_dispatches` |
| L4 | 没有飞书应用凭据时不建立伪连接，健康检查明确报未配置 | `test_unconfigured_long_connection_stays_fail_closed` |
| L5 | Channel SDK 不接管或停止 Uvicorn 的 ASGI 主事件循环 | `test_sdk_ws_loop_isolated_from_running_asgi_loop` |

交付时已在 Python 3.11.14 虚拟环境安装 `lark-channel-sdk 1.4.0`，
并在真实飞书租户与测试群完成 WebSocket 连接、群指令和告警卡片验收。
卡片按钮点击与真实预约写入仍需在正式业务流程中单独验收。

## 安全边界兜底

| 边界 | 对应测试 |
|---|---|
| 协调服务对集群完全没有写能力 | `TestSafetyBoundaries::test_no_path_ever_produces_a_machine_command` |
| 采集异常永远不会变成「可以使用」 | `TestSafetyBoundaries::test_unknown_never_reaches_business_chat_as_available` |
| 采集脚本任何路径都不执行改状态的命令 | `test_agent.py::TestReadOnlyBoundary::test_no_mutating_command_in_any_path` |
| `docker exec` 只用于 `pgrep` | `test_agent.py::TestReadOnlyBoundary::test_docker_exec_is_only_used_for_pgrep` |
| GPU 满载/空载都不改变判定 | `test_agent.py::TestGpuNeverDecides` |
| 超时一天也不释放 | `test_coordinator.py::TestTimeoutScan::test_never_auto_releases_however_long` |

---

## 现场人工验收（自动化替代不了的部分）

自动化测试用的是飞书替身。以下必须在真实测试群走一遍：

### 环境准备

- [ ] 业务机器人已加入监控群和业务测试群
- [ ] 应用已获得读取群消息权限，`FEISHU_MONITOR_POLL_SECONDS` 大于 0
- [ ] `.env` 中群 ID、机器人 open_id、管理员 open_id 均已填写
- [ ] 预约 JSON 里有真实的 `current_open_id` 和 `next_open_id`

### 链路验收

- [ ] **A1** 集群脚本 `--once --print-event` 输出的 `EVENT_JSON` 能被服务解析
      （`curl -X POST /api/ingest` 返回 `accepted: true`）
- [ ] **A2** 监控群里能看到 Webhook 消息，正文含完整 `EVENT_JSON`
- [ ] **A3** 等待一个轮询周期后，`/api/health` 的 `feishu_monitor_relay.last_ingested`
      大于 0，且大屏采集时间已更新
- [ ] **A4** 制造疑似空闲（停掉 pytest、停止写 `/data1`，容器保持运行），
      15 分钟后业务群收到占用确认卡片，且 `@` 的是当前占用人
- [ ] **A5** 点「继续使用 30 分钟」→ 卡片变成已处理态、按钮消失，
      大盘显示「保留还剩 ~30 分钟」
- [ ] **A6** 再点同一张卡片 → 飞书弹出「这张卡片已经处理过了」
- [ ] **A7** 换一个人点卡片 → 弹出「已失效，或者你不是当前占用人」，预约未变
- [ ] **A8** 点「已使用完」→ 业务群 `@` 到下一位，**集群上的容器和进程原封不动**
- [ ] **A9** 发出卡片后不回复，10 分钟收到提醒，20 分钟管理员被 `@`，
      **集群仍属于当前占用人**
- [ ] **A10** 停掉集群侧 agent，20 分钟后大盘该卡片标注「数据可能过期」，
      状态没有变成空闲
- [ ] **A11** 把 agent 账号移出 docker 组 → 状态变 `UNKNOWN`，
      管理员被告警，**业务群没有任何「可以使用」消息**
- [ ] **A12** 关掉监控群 webhook，只留直连通道 → 全流程仍然正常

### 回归确认

- [ ] 连续观察 24 小时，业务大群的消息条数与人工交接次数相当（不刷屏）
- [ ] 灰度期本地 JSONL 与人工使用记录对照，误判率可接受
- [ ] `journalctl -u hil2-coordinator` 中没有反复出现的 ERROR
