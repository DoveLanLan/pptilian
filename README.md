# 提链工具源码包

Created: 20260811-194825
Source project copied read-only from:
C:\Users\16218\session-link-gen-sessionpool-final-500-20260628-204811\session-link-gen-sessionpool-final-500-momo-restored-20260730-1350

说明：这是根据原项目复制出来的新源码包；原项目副本本身未被回写或改动。当前独立源码包额外加入了启动时展示的“小脑虎 GPT 技术交流群”二维码弹窗、二维码静态资源与对应页面契约测试。

## 交流群

扫码加入小脑虎 GPT 技术交流群：

![小脑虎 GPT 技术交流群二维码](static/images/startup-community-qr.png)

包含范围：
- 提链工具后端入口：app.py 中 /api/payment-modes、/api/generate-link、并发任务/日志相关逻辑
- 提链核心：core.py 中 PAYMENT_MODES、generate_payment_link、各支付/PayPal全球轮转/OAICS/cs_live 分支
- 提链 UI：templates/index.html 中“提链工具”页面、PAYPAL全球轮转配置区及启动二维码弹窗
- 启动二维码资源与测试：static/images/startup-community-qr.png、tests/test_startup_qr_modal.py
- 提链运行依赖辅助：upi_high_success.py、pix_standalone_extract.py、sentinel*.py、openai_sentinel_quickjs.js、http_client.py、config.py、static/js/sff_core.js

排除范围：
- 未复制 services/ 目录
- 未复制 free-headless-registration
- 未复制 mailbox/webui/协议控制台等其它服务目录
- 未复制数据、账号、代理、日志、缓存、venv、secrets

注意：app.py/index.html 是原项目的综合入口文件，已按原样复制用于源码追溯；本包没有接入其它服务目录。

## 前置代理（FRONT_PROXY）

当代理池里的落地 IP **无法直连**、必须先经本地前置 SOCKS 代理中转时（链路 `本地 → 前置代理 → 代理池落地IP → 目标站`），通过环境变量 `FRONT_PROXY` 启用 GOST 两跳桥接：

```bash
# 一键启动（自动建 venv / 装依赖 / 装 gost v2 到 bin/，默认前置代理 socks5://127.0.0.1:10808）
./start.sh

# 自定义前置代理 / 端口
FRONT_PROXY=socks5://127.0.0.1:10809 PORT=8080 ./start.sh
```

- 配置了 `FRONT_PROXY` 后，代理池里选中的落地 SOCKS 代理一律走 GOST **两跳**：
  `gost -L http://127.0.0.1:随机端口 -F <前置代理> -F <落地代理>`；
  页面「检测代理池」的链路标签会显示 `前置代理 -> 落地IP -> http://127.0.0.1:端口`。
- 未配置 `FRONT_PROXY` 时行为与原版完全一致（GOST 单跳或直连）。
- 代理池条目：**没写协议前缀的一律按 HTTP 处理**（如 `user:pass@host:80` 或 `host:80:user:pass`）；是 SOCKS 的必须显式写 `socks5h://`。
- 依赖 gost v2 可执行文件：`start.sh` 会自动下载到 `bin/gost`（macOS/Linux）；
  Windows 请下载 `gost.exe` 放到项目目录或 `bin\` 目录
  （[ginuerzh/gost v2.12.0](https://github.com/ginuerzh/gost/releases/tag/v2.12.0)）。
- macOS 上 5000 端口可能被 AirPlay 占用，`start.sh` 会自动顺延选择空闲端口（也可用 `PORT` 覆盖）。
