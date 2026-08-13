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
