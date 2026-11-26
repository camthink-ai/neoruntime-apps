# RS-485 接收功能测试指南

给测试同事的实操指南：怎么搭环境、怎么用工具、每条用例怎么做、什么算通过。
工具本身的说明见 `README.md`。

## 1. 测什么

应用调用一次订阅后，RS-485 总线上收到的每一帧都应以事件到达 SDK；队列深度和
丢帧策略可控。

NE503 **不接收自己发出的数据**，所以"收帧"必须由 PC 发、"发帧"必须由 PC 看。
两件事都由 `rs485_peer.py` 完成。

```
   PC + USB-RS485 适配器                      NE503
  +----------------------+   RS-485 总线   +---------+   device-control   +--------------+
  | rs485_peer.py        |   A --- A       |   MCU   |   SubscribeRs485Rx| 测试应用      |
  |   send / echo /      |   B --- B       | 按空闲   |------------------>| 网页控制台    |
  |   monitor / raw      |   GND - GND     | 切帧    |    Rs485Tx        | 端口 8097     |
  +----------------------+                 +---------+<------------------+-------------- |
                                             （每帧 ≤ 512 字节）
```

## 2. 被测件

| 部件 | 要求 | 本次版本 |
| --- | --- | --- |
| 平台应用 | 带 RS-485 接收流 RPC（平台 `release/v1.2.0` 及以后） | 【待填】 |
| MCU 固件 | v0.1.9 | 【待填】 |
| SDK | 0.8.2 及以后（已打进工具镜像） | 0.8.2 |
| 测试工具 | `rs485-testbench-0.1.0-arm64.neoapp`、`rs485_peer.py` | 0.1.0 |

## 3. 环境

- NE503 设备一台，PC 一台（Windows 或 Linux，Python 3.8 以上）。
- USB-RS485 适配器一个。接线：适配器 **A 接设备 A，B 接设备 B，GND 共地**。
  A、B 接反时收不到任何帧。
- PC 能通过网络访问设备（浏览器打开工具页面）。

### 3.1 把工具装到设备上

1. 浏览器登录设备的 Web 控制台，在应用页面导入 `rs485-testbench-0.1.0-arm64.neoapp`。
   也可以解包后执行
   `aipc-cli app install rs485_testbench <解包目录>/app.yaml <解包目录>/image.tar`。
2. 启动应用 **RS-485 Testbench**。
3. 浏览器打开 `http://<设备地址>:8097/`。右上角应显示 `sdk 0.8.2`。

### 3.2 准备 PC 对端

```bash
pip install pyserial
python rs485_peer.py ports          # 记下适配器的端口，Windows 如 COM6，Linux 如 /dev/ttyUSB0
```

下文的 `COM6` 请换成实际端口。串口同一时间只能被一个程序打开：用脚本时先关掉
MobaXterm 等串口工具。

## 4. 页面说明

| 区域 | 控件 | 作用 |
| --- | --- | --- |
| Bus and stream | Baud、Backlog、drop oldest when full | 波特率、队列深度、队列满时丢哪一端。后两项在下次 Start stream 时生效 |
| | Slow consumer | 每收一帧故意延迟多少毫秒，用来模拟读得慢的应用 |
| | Apply / Init / Deinit | 保存设置 / 初始化串口 / 释放串口 |
| | Start stream / Stop stream | 开始、停止订阅 |
| | Start 2nd subscriber | 再开一个独立订阅（队列 128，不限速），用来验证订阅者之间互不影响 |
| Frames | 表格 | 收到的帧，最新的在最上面：序号、时间、与上一帧的间隔、长度、类型、向量号、内容 |
| Run verdict | 徽标和统计 | 对端发来一组编号帧后的判定：PASS / FAIL，以及收到的、丢失的、损坏的、重复的、乱序的数量 |
| Transmit | Payload + Send | 让设备发一帧手工内容（文本或十六进制） |
| | Numbered burst | 让设备连发一组编号帧，由 PC 的 `monitor` 检查 |
| Round trip | Run round trip | 设备发 ping、PC 回显，测往返时间 |
| 右上角 | Clear / Download report | 清空计数 / 下载本次结果的 JSON |

## 5. 每条用例开始前

1. 页面点 **Clear**。
2. 确认右上角 `bus: inited @ <波特率>`、`stream: active`。不是的话：选波特率 →
   Apply → Init → Start stream。
3. PC 上执行对端命令。
4. 对端发完后，页面约 2 秒给出 Run verdict。

## 6. 用例

| 编号 | 目的 | 步骤 | 预期 |
| --- | --- | --- | --- |
| RX-01 | 基本收帧 | `raw --text "OPEN 1"` | Frames 出现 1 行：类型 raw，长度 6，text 为 `OPEN 1` |
| RX-02 | 二进制和中文内容 | `raw --hex "00 ff 7f 80 01"`；再 `raw --text "开门"` | 第一帧长度 5，hex 与输入一致；第二帧长度 6，hex 是这段文字的 UTF-8 编码 |
| RX-03 | 编号帧完整性 | `send --count 300 --size 64 --interval-ms 5` | Run verdict 为 **PASS**，received 300，missing 0 |
| RX-04 | 长度边界 | `send --count 10 --size 1`；`send --count 10 --size 499` | 两次都 PASS。第二次每帧长度 512（载荷 499 + 13） |
| RX-05 | 超长连续流（探索性） | 发 1030 字节，命令见下 | 按设计应拆成多帧，每帧不超过 512，各帧字节数之和为 1030，拼接后与发送内容一致。**这条尚未在实机确认，请记录实际现象** |
| RX-06 | 波特率 | 页面选 9600 → Apply → Deinit → Init → Start stream；`--baud 9600 send --count 100 --size 16`；再换回 115200 重做 | 两种波特率下都 PASS |
| RX-07 | 多帧不粘包、不乱序 | `send --count 200 --size 32 --interval-ms 20` | PASS；Frames 里每行长度都是 45（载荷 32 + 13） |
| RX-08 | 丢帧策略：丢最旧 | 勾选 drop oldest，Slow consumer 填 200，Apply，Stop 再 Start stream；`send --count 60 --size 16 --interval-ms 2` | **FAIL**，missing 大于 0；kept 一栏是 `head kept, tail kept`（丢的是中间的旧帧） |
| RX-09 | 丢帧策略：保序（丢最新） | 取消勾选 drop oldest，Stop 再 Start stream；同上发 60 帧 | **FAIL**；kept 一栏是 `head kept, tail dropped` |
| RX-10 | 队列深度 | Backlog 选 128，Slow consumer 仍为 200，Stop 再 Start stream；同上发 60 帧 | **PASS**：128 的队列装得下，不再丢。Backlog 选 default (16) 时结果应与 RX-08 一致 |
| RX-11 | 订阅者互不影响 | Backlog 改回 16，Slow consumer 200，Stop 再 Start stream；点 Start 2nd subscriber；发 60 帧 | 主 Run verdict 为 **FAIL**（missing 大于 0）；2nd subscriber 的 run 为 **PASS**，got 60 of 60 |
| RX-12 | 停掉一个订阅不影响另一个 | 点 Stop stream；`send --count 20 --size 16` | 2nd subscriber 的 run 为 PASS；主 Frames 不再增加 |
| RX-13 | 设备发送（回归） | PC 先开 `monitor`；页面 Numbered burst 填 Frames 200、Payload 32、Gap 20，点 Start burst；发完后再在 Payload 输入 `OPEN 1` 点 Send | PC 的 `monitor` 最后打出 `VERDICT: PASS`，vectors 200，missing 0；并显示一帧长度 6、内容 `OPEN1` 的原始帧。按 Ctrl+C 退出 |
| RX-14 | 回环时延 | PC 开 `echo`；页面 Round trip 填 Pings 50、Payload 16、Gap 100、Timeout 1000，点 Run round trip | Result 里 lost 为 0；把 min、avg、p50、p99、max 记入结果表。**目前没有验收阈值，只记录** |
| RX-15 | 服务重启后恢复（需研发配合） | 保持订阅，SSH 到设备执行 `systemctl restart camera-daemon`；恢复后再 `send --count 50` | 重启在 30 秒内完成（不是卡到 90 秒被强杀）；页面 stream 回到 active；send 之后 PASS |
| RX-16 | 稳定性 | `send --count 3600 --size 32 --interval-ms 1000`（约 1 小时） | 期间 stream 没有 error；最后一次 run 为 PASS；1 小时内页面和设备响应正常 |
| RX-17 | 未初始化、释放后的行为 | Deinit；`send --count 20`；再 Init 后 `send --count 20` | Deinit 后页面没有新帧，也不报错；Init 后恢复，PASS |
| RX-18 | 重复 Init（已知行为） | 已 Init 时再点 Init | 页面提示 `Init failed: …`，错误码 -2815；先 Deinit 再 Init 则成功 |

RX-05 的发送命令（发 1030 个字节 `0x41`）：

```powershell
# Windows PowerShell
python rs485_peer.py --port COM6 raw --hex ('41' * 1030)
```

```bash
# Linux / macOS
python rs485_peer.py --port /dev/ttyUSB0 raw --hex "$(python -c "print('41' * 1030)")"
```

## 7. 结果记录

每条用例记录：编号、实际结果、是否通过、证据（页面的 Download report 导出的
JSON，必要时加截图）。RX-14 请把五个时延数值填进结果表。

## 8. 已知行为（不算缺陷）

- **可能重复**：投递语义是 at-least-once。MCU 到主机的链路如果丢了确认，会重发
  同一帧，此时会看到 duplicates 加 1。
- **帧长上限 512 字节**：超过的连续数据会拆成多帧。
- **时延数值包含 PC 适配器的 USB 延时**：回环时延是"设备发送 + 总线 + PC 适配器
  + 设备接收"的全程，同一台 PC、同一个适配器下对比才有意义。
- **重复 Init 报错**：见 RX-18。
- **页面的 Round trip 需要 PC 处于 echo 模式**，否则 lost 等于 Pings。

## 9. 排障

| 现象 | 先看什么 |
| --- | --- |
| 页面打不开 | 应用是否已启动；端口是否 8097；`aipc-cli app logs rs485_testbench` |
| `cannot open COM6` | 串口被别的程序占用，或端口号不对；`python rs485_peer.py ports` |
| 发了但页面没有帧 | 页面是否 `bus: inited` 且 `stream: active`；两边波特率是否一致；A、B 是否接反 |
| 页面 stream 变成 `error … UNIMPLEMENTED` | 平台固件太旧，没有 RS-485 接收流 RPC |
| Frames 里有内容但是乱码 | 两边波特率不一致 |
| `VERDICT: NONE` | PC 的 `monitor` 没收到编号帧：先在页面 Start burst，再开 `monitor` |
| Run verdict 一直停在 receiving… | 对端还没发完，或丢了公告帧；等 2 秒，或点 Clear 后重发 |
