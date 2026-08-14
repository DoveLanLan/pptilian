# PayPal 提链完整流程分析(源码向)

> 适用范围:本仓库 `pptilian`(core.py / app.py / templates/index.html)中所有以 **PayPal 开头的模式**
> (`PayPal 长链接 US/USD`、`PayPal 长链接 FR/EUR`、`PayPal 长链接 BR/BRL`、
> `PAYPAL全球轮转`、`PayPal全球无优惠提链`、以及改版的 `无卡长链接 US/USD`)。
>
> 本文帮你搞清楚三件事:**① 提链到底在干嘛;② 一条链是怎么从无到有跑出来的;③ 为什么这几天你一条都没提出来,以及怎么提高成功率。**

---

## 一、先说结论(30 秒看懂)

**提链 = 用一个 ChatGPT 账号的 AccessToken,冒充浏览器去 OpenAI 下单,但不真的付款,在支付环节把「PayPal 授权页链接」截取出来。**

这条链接长这样:

| 类型 | 样子 | 用途 |
|---|---|---|
| PayPal BA approve 链(最优) | `https://www.paypal.com/agreements/approve?ba_token=xxxxxxxx` | 买家点开即可授权,无需进购物车 |
| PayPal 登录/授权入口页 | `https://www.paypal.com/.../signin?...&ba_token=...` 或带 `agreements/approve` 的页面 | 也能用,相当于进入 PayPal 授权流程 |
| pm-redirects 真链(无卡US 模式) | `https://pm-redirects.stripe.com/...` | Stripe 的支付跳转链,也算成功 |
| 假链 / 废链(必须拒绝) | `https://pay.openai.com/c/pay/cs_live...`、paypal 静态资源、`paypalobjects` 素材 | 页面表单,点了没结果,程序会主动跳过 |

**核心逻辑只有一个:Stripe 确认支付后,后端会返回一条「跳转到 PayPal 授权页」的 302 链接,程序一路跟随跳转,抓到 `paypal.com` 带 `ba_token` 的网址就算成功。** 所谓「提链成功率低」,本质是这条 302 链没被产生出来、或产生后被代理/风控拦掉了。

---

## 二、完整流程(7 步拆解)

入口:`/api/generate-link`(或 `/api/start-retry`) → `_generate_with_retries()`(app.py) →
`generate_payment_link()`(core.py) → 分发到 `generate_opll_paypal_long_link()`(core.py:8120,PayPal 长链接模式)
或 `generate_opll_paypal_global_rotation_link()`(core.py:8657,PAYPAL全球轮转模式)。

下面以 **PayPal 长链接 XX/XX** 为例,完整 7 个阶段(`_emit_payment_stage` 里标注的阶段号):

```
用户输入
 ├─ AccessToken(必填,关键)
 ├─ 代理池(Checkout 入口 / Stripe-PayPal 出口 / 优惠代理)
 └─ 模式、国家、币种、Locale、Email(可选)
        │
        ▼
┌────────────────────────────────────────────────────────────────────┐
│ ① 创建 ChatGPT checkout             (checkout 代理 / 入口代理)      │
│    POST chatgpt.com/backend-api/payments/checkout                    │
│    产出: cs_id(cs_ 开头)或 oaics_(OpenAI 自定义会话)                │
├────────────────────────────────────────────────────────────────────┤
│ ② 初始化 Stripe 支付页               (Stripe-PayPal 代理 / 出口代理) │
│    POST api.stripe.com/v1/payment_pages/{cs_id}/init                │
│    产出: stripe_hosted_url、stripe pk、金额、currency                │
│    ⚠ 这里必须能看到 payment_method_types 里有 PayPal                │
├────────────────────────────────────────────────────────────────────┤
│ ③ 创建 PayPal PaymentMethod         (出口代理)                      │
│    用目标国账单信息在 Stripe 里建一个 paypal PM                     │
├────────────────────────────────────────────────────────────────────┤
│ ④ Stripe confirm 确认               (出口代理)                      │
│    把 PM 绑到 checkout 上,产出 confirm_payload                      │
├────────────────────────────────────────────────────────────────────┤
│ ⑤ ChatGPT approve / 等授权          (checkout 代理 / 入口代理)      │
│    若 state=requires_approval → 调 approve 接口放行                 │
│    产出: stripe_redirect_url(Stripe 返回的跳转链)                    │
├────────────────────────────────────────────────────────────────────┤
│ ⑥ 跟随 PayPal redirect              (出口代理)                      │
│    一路跟 302,最多 5 跳,直到命中 paypal.com                         │
│    失败则从 confirm/init 返回体里反查候选 PayPal URL                │
├────────────────────────────────────────────────────────────────────┤
│ ⑦ 校验 + 收尾                                                       │
│    验证是「真 PayPal 链」→ 返回 long_url(否则判失败)                │
└────────────────────────────────────────────────────────────────────┘
```

### 各阶段详细说明(以及"会卡死在哪")

**① 创建 checkout** — `opll_create_checkout()` (core.py:1551)
- 请求体:`plan_name=chatgptplusplan` + `billing_details`(目标国) + `promo_campaign`(默认 `plus-1-month-free` 优惠)。
- 走 **入口代理**(Checkout 代理)。AccessToken 失效 → 401;代理被 ChatGPT 拦 → 403(尤其注意返回的是 **HTML 而非 JSON** 的 403,那是出口被目标路由风控了)。
- 产出 `cs_id`。**`cs_` 开头 = Stripe checkout;`oaics_` 开头 = OpenAI 自定义 checkout**(PAYPAL全球轮转里,BR/TH 主代理会优先走 oaics 分支,因为 hosted 会被 Stripe 按 IP 归属地隐藏 PayPal)。

**② Stripe init** — `opll_build_stripe_session()` + `opll_stripe_init()` (core.py:1945)
- 走 **出口代理**(Stripe-PayPal 代理)。这一步是**风控重灾区**:Stripe 会按出口 IP、账单国、Locale、浏览器指纹决定"这个 checkout 开放哪些支付方式"。
- **关键检查:`opll_payment_method_available()` 确认 `payment_method_types` 里有 `paypal`,没有就直接抛错失败。** 这就是最常见的"代理能用但提不到链"——paypal 压根没对你开放。

**③ 创建 PayPal PM** — `opll_stripe_create_paypal_method()` (core.py:2973)
- 用目标国账单地址(pm_country)在 Stripe 建 PayPal PaymentMethod。邮箱不合法、地址字段空(如 `tax_region[line2]`)都会失败。

**④ Stripe confirm** — `opll_stripe_confirm()` (core.py:4858)
- 把 PM confirm 到 checkout。失败 = Stripe 参数/风控问题。

**⑤ ChatGPT approve / 等授权** — `opll_redirect_url_after_confirm()` (core.py:5566)
- 如果 confirm 返回 `state=requires_approval`,调用 `opll_chatgpt_approve_with_retry()` 去放行(走 **入口代理**),然后轮询 Stripe 拿 redirect。
- **这里就是 `approval_blocked` / `approval_requires_approval_timeout` 两类错误的来源**:approve 请求被风控挡住、或 Cookie 与账号不匹配,导致订单一直"等授权"不放行。
- 拿到 `stripe_redirect_url`(Stripe 给的跳转链)。

**⑥ 跟随 PayPal redirect** — `opll_resolve_external_redirect()` (core.py:4593)
- 用出口代理 follow 最多 5 跳 302,目标是落在 `paypal.com`。
- 没跟到就把 confirm/init 返回体翻一遍(`opll_extract_paypal_candidate_url`)找候选链接。
- **BR 模式额外一步:`opll_paypal_url_for_country()` 把页面强制成巴西**(`country.x=BR&locale.x=pt_BR`)。

**⑦ 校验成功** — 判断标准见下一节。不满足就抛错 → 进入重试循环。

### 重试循环怎么转的

`_generate_with_retries()` (app.py:1168):
- 默认 **1 次**(`max_attempts`),上限 500;并发默认 1,上限 100。UI 里可以调「尝试次数 / 并发线路数」。
- **每轮重试换一个代理**(从代理池 `random.choice`),但**一轮之内的 checkout→Stripe→approve 保持同一组代理**("sticky")。
- 每次失败会记录 `error`,并按 `_classify_attempt_error()` (app.py:963) 归类成 UI 上的诊断分类(见第六节)。
- 全部尝试用完 → 返回 `ok:false`,错误列表最后 10 条。

---

## 三、成功标准:什么样的 URL 才算"提到链了"

判定函数都在 core.py:3546–3636,`opll_is_paypal_success_url()` 是总开关:

```
成功 = 命中「PayPal 授权入口链」 或 (宽松模式下命中「任意真 paypal.com 页面」)
```

- **PayPal BA approve 链(最严格、最优)** — `opll_is_paypal_ba_approve_url()`:
  域名是 `paypal.com` 且路径是 `/agreements/approve` 且带 `ba_token` 参数。
- **PayPal 授权入口页(次优)** — `opll_is_paypal_approval_entry_url()`:
  paypal.com + (出现 `agreements/approve` / `ba_token` / `billingagreement`) 且 (出现 `/signin` `/login` `/webapps/hermes` 或 `return`)。
- **宽松"任意 PayPal 页"(只有 BR 用)** — `opll_is_paypal_page_url()`:
  BR 模式 `paypal_result_mode=paypal_link` 是 **loose** 的,只要是真 paypal.com 页面(排除图片/css/js 素材)就算成功。因为巴西只要求"拿到一个真实 PayPal 页面链"。
- **pm-redirects(只有无卡 US 用)** — `opll_is_pm_redirect_url()`:
  域名是 `pm-redirects.stripe.com`,无卡 US 模式把它当成功。

**程序会主动拒绝的假链:**
- `pay.openai.com/c/pay/cs_live...`(纯表单页,点了没结果);
- `paypalobjects.com` 静态资源、图片/css/js;
- 只有 Stripe 资源 URL 而没有任何 PayPal 链。

> 一句话:你的模式决定了"松/严"。**US/FR 要的是真授权链,BR 只要真 PayPal 页。** 无论哪种,`pay.openai.com` 假链都会被跳过并报"跳过假链"。

---

## 四、代理体系:为什么提链需要好几路代理

这是整套工具最容易被搞混的地方,也最影响成功率。链路设计如下:

```
你的电脑
  └─ 本地前置代理 FRONT_PROXY(可选,如 socks5://127.0.0.1:10808)
       └─ GOST 两跳桥接(可选)
            └─ 代理池落地 IP(Checkout入口 / Stripe-PayPal出口 / 优惠代理)
                 └─ 目标站 chatgpt.com / api.stripe.com / paypal.com
```

### 三种"代理"角色(app.py 里的字段)

| UI/请求字段 | 代码角色 | 服务对象 |
|---|---|---|
| `payment_proxy_pool`(ppp) | **入口代理 / Checkout 代理** | `chatgpt.com`(建 checkout + approve 放行) |
| `provider_proxy_pool` / `paypal_proxy_pool`(ppp2) | **出口代理 / Stripe-PayPal 代理** | `api.stripe.com` + `www.paypal.com`(init/PM/confirm/跟 redirect) |
| `paypal_promo_proxy_pool`(优惠代理) | **优惠代理**(仅全球轮转) | checkout/update 刷 0 元优惠 |

规则:
- **没写协议前缀的一律按 HTTP**(如 `user:pass@host:80` 或 `host:80:user:pass`);是 SOCKS 的**必须显式写 `socks5h://`**。
- **检测代理池**(`_test_proxy_candidate` app.py:1977)会分别测 `chatgpt.com` / `api.stripe.com` / `paypal.com`(BR 拆分模式) 三个站点连通性,再用 `ipinfo.io` 看出口 IP 国家。
- **IP 国家必须和目标匹配**:PayPal US 就要 US 出口;PayPal BR 就要 BR 出口。**出口 IP 国家不对 → Stripe 按错误国家展示支付方式 → 看不到 PayPal → 提不到链。** 这是头号原因。

### BR 特有的"域名拆分路由"(`_build_paypal_br_domain_routes` app.py:924)

巴西家庭网关代理有个怪毛病:**能放行 Stripe/PayPal 的 CONNECT,却重置 chatgpt.com 的 CONNECT**。所以 BR 模式配置了 `FRONT_PROXY` 后,路由被拆成两条:

```
ChatGPT checkout/approve → FRONT_PROXY(本地前置,socks5://127.0.0.1:10808)
Stripe/PayPal           → BR 网关代理直接连
```

对应 `paypal_br_split` 路由模式。**所以 BR 模式:前置代理 + 巴西落地代理都要配,缺一不可。**

### 什么时候用 GOST 两跳

- 代理池落地 IP **无法直连**(必须先经本地前置代理中转)时,配置 `FRONT_PROXY` 启用 GOST 两跳桥接。
- `start.sh` 会自动下载 gost v2 到 `bin/`,链路标签显示 `前置代理 -> 落地IP -> http://127.0.0.1:端口`。
- 注意:**无卡US / Team Codex / PayPal全球轮转 这些模式强制 `use_gost_bridge=False`,用直连 socks5h**;split 阶段模式(荷兰/iDEAL/UPI/PIX 等)强制走 GOST。

---

## 五、各 PayPal 模式对照表

| 模式 | 走哪个函数 | 出口要求 | 成功标准 | 是否要优惠代理 | 备注 |
|---|---|---|---|---|---|
| `PayPal 长链接 US/USD` | `generate_opll_paypal_long_link` | US 出口 | 严格 BA approve / 授权入口 | 否 | 可选择 方案① 全流程日本 / 方案② JP checkout+US PayPal(`paypal_strategies`) |
| `PayPal 长链接 FR/EUR` | 同上 | FR 出口 | 严格 BA approve / 授权入口 | 否 | 无策略选项 |
| `PayPal 长链接 BR/BRL` | 同上(loose) | BR 出口 + 前置代理 | **宽松**:任意真 PayPal 页 | 否 | 会强制页面 `country.x=BR` |
| `PAYPAL全球轮转` | `generate_opll_paypal_global_rotation_link` | 主 PayPal 代理池 + 优惠代理池 | 优惠归零 + 拿到 BA/PM 链 | **是,必填** | 21 国账单任选;BR/TH 主代理优先走 OAICS custom 分支 |
| `PayPal全球无优惠提链` | 同上(`apply_promotion=False`) | 只需 PayPal 代理池 | BA/PM 链 | 否 | 不做优惠刷 0 |
| `无卡长链接 US/USD` | `generate_opll_paypal_long_link`(`pm_or_paypal`) | US 出口 | pm-redirects 或 BA approve | 否 | 改版后不再返回 pay.openai.com 假链 |

---

## 六、为什么这几天一条都没提到?——失败原因按概率排查

`_classify_attempt_error()` 把错误分成了几十类。按实际提链场景,**按这个顺序排查最有效**(前面几条命中率最高):

### 🔴 头号原因(占 80%+):代理 / IP 不对

| 现象 | 代码判定 | 含义 | 怎么办 |
|---|---|---|---|
| `Stripe checkout did not expose PayPal` / `paypal advertised list missing` | `paypal_explicit_pm_stage` | **出口 IP/账号组合下,Stripe 根本没给这个 checkout 开放 PayPal** | 换出口代理(换国家/换机房/换运营商);确认出口 IP 国家与目标一致;换账号重试 |
| `checkout create failed: http 403`(返回 HTML) | `checkout_edge_forbidden` | 出口被 ChatGPT 目标路由风控拦截 | 换入口代理;查 AccessToken 是否还有效(无效会看到 401/JSON 认证错) |
| `curl: (35)` / `TLS connect error` / `unexpected_eof` / `record layer failure` / `RemoteDisconnected` / 各种 `timed out` | `network_or_proxy_timeout` | 代理线路烂,TLS 握手/连接被断 | 换线路;HTTP 代理换 SOCKS,或反之;加大尝试次数 |
| `检查代理池`测出来 `chatgpt_status/stripe_status/paypal_status` ≥500 或连不通 | — | 代理根本到不了目标站 | 先点"检测代理池",只看 `ok:true` 且对应站点状态码正常的代理 |

### 🟠 二号原因:账号 / Cookie / Token 问题

| 现象 | 代码判定 | 怎么办 |
|---|---|---|
| `access token` / `unauthorized` / `http 401` | `auth_or_cookie_error` | Token 过期/失效,**换账号** |
| `approval 被 blocked` / `result='blocked'` | `approval_blocked` | ChatGPT approve 被风控挡住,自动放行失败 |
| `approval 错误` / `chatgpt approve failed` | `approval_error` | **填上同账号的浏览器 Cookie**(UI 的 Cookie 输入框)再跑 |
| `approval requires_approval timeout` | `approval_requires_approval_timeout` | 订单一直"等授权"不放行 = 极高概率 approve 被拦;换账号 + 加 Cookie |

### 🟡 三号原因:优惠 / 金额阶段(仅全球轮转)

| 现象 | 代码判定 | 怎么办 |
|---|---|---|
| `checkout/update promotion failed: http 403` | `promotion_http_403` | **优惠代理被拒**(TR/VN 优惠代理常被 403);换优惠代理,加同账号 Cookie |
| `oaics 优惠未归零` / `amount is not zero` | `amount_not_zero` | 优惠没刷到 0 元;换优惠代理或换账号 |

### ⚪ 四号:其他

- `Stripe parameter error` / `email_invalid`:生成的账单邮箱不合法 → 手动填个合法邮箱。
- `payment_method_types_mismatch ... paypal`:已经走到 confirm 阶段了但 Stripe 说不匹配 → 换账号/代理再来一轮。

> **提示:去看前端页面的错误日志(attempts_log),上面直接显示 `_classify_attempt_error` 的诊断文案,一眼就能定位是哪一类。**

---

## 七、提高提链成功率的 12 条实操建议

**代理是最大变量,先解决代理,再谈技巧:**

1. **先跑"检测代理池",只用三项全绿的代理。** 点检测后,过滤掉 `chatgpt_status`/`stripe_status`/`paypal_status` 非 200/302 的;BR 模式还要确认 `country=BR`。
2. **出口 IP 国家必须与模式匹配。** US 模式用 US 出口、BR 用 BR 出口。**宁可少,不要错**——一个错国家的代理 100% 提不到。
3. **用「有标 SID 的住宅代理」且每个代理尽量独立/独享。** 共享出口/被很多人刷过的 IP,Stripe 风控直接不开 PayPal。`randomize_proxy_sid()` 会帮你给住宅代理随机 SID。
4. **BR 模式必须配 `FRONT_PROXY`。** 不配就没有 `paypal_br_split` 路由,BR 网关可能重置 chatgpt.com,checkout 都建不出来。

**账号/输入层面:**

5. **AccessToken 务必是"能正常付费"的活跃账号**,不要用已过期/被封/白嫖过头的号。
6. **尽量填同账号的浏览器 Cookie**(UI 有输入框)。approve 放行、OAICS 优惠阶段非常依赖它;不填 = 订单可能一直 requires_approval。
7. **账单邮箱手动填合法邮箱**(不要用带空格/标点的默认生成邮箱,Stripe 会 `email_invalid` 拒绝)。
8. **PAYPAL全球轮转:账单国、Locale、主代理国、优惠代理国四个维度对齐。** 比如选 JP 账单 → 主代理尽量 JP、优惠代理选干净线路;BR/TH 主代理会自动走 OAICS 分支,需要 Cookie 配合。
9. **同时勾选 方案①(全流程日本)+ 方案②(JP checkout + US PayPal)** 两条策略并发跑,成功面更大(并发会自动抬到 ≥2)。

**跑法/预期管理:**

10. **把"尝试次数"从默认 1 调高(如 10–30),开并发(如 5–10 线)。** 提链本来就是概率游戏,单次尝试失败是常态,量上去才谈得上出链。
11. **换个模式/换个国家试。** 某个模式"这几天一条没有",很可能是**某个出口国家被 Stripe 阶段性收紧**(US 收紧就试 JP/FR;全球轮转就换账单国)。这比死磕一个模式有效得多。
12. **关注提链日志里的 error 分类**,别盲跑。看到 `paypal advertised list missing` 就换代理、换账号;看到 `approval blocked` 就加 Cookie、换账号。对症下药,一次一个变量。

---

## 八、自检清单(跑之前过一遍)

```
[ ] AccessToken 能正常登录且为可付费账号
[ ] 模式与出口代理国家一致(US→US 出口,BR→BR 出口)
[ ] 检测代理池:chatgpt / stripe / paypal 三项全通,出口国家正确
[ ] (BR)已配置 FRONT_PROXY,能看到 paypal_br_split 链路
[ ] (全球轮转)优惠代理已填,且不是已知被 403 的线路
[ ] 已填同账号浏览器 Cookie(强烈建议)
[ ] 账单邮箱格式正确
[ ] 尝试次数 ≥ 10,并发 ≥ 5(概率游戏要靠量)
[ ] 失败后先看日志分类,再决定换代理还是换账号
```

---

## 附录:关键代码位置速查

| 内容 | 位置 |
|---|---|
| 提链总入口(带重试) | `app.py:_generate_with_retries` (1168) |
| 支付模式表 | `core.py:PAYMENT_MODES` (103) |
| PayPal 长链接 7 步流程 | `core.py:generate_opll_paypal_long_link` (8120) |
| PAYPAL全球轮转流程 | `core.py:generate_opll_paypal_global_rotation_link` (8657) |
| 建 checkout | `core.py:opll_create_checkout` (1551) |
| Stripe init | `core.py:opll_stripe_init` (1945) |
| 建 PayPal PM | `core.py:opll_stripe_create_paypal_method` (2973) |
| Stripe confirm | `core.py:opll_stripe_confirm` (4858) |
| approve/等授权 | `core.py:opll_redirect_url_after_confirm` (5566) |
| 跟随 PayPal redirect | `core.py:opll_resolve_external_redirect` (4593) |
| 成功判定 | `core.py:opll_is_paypal_success_url` (3633) |
| BR 域名拆分路由 | `app.py:_build_paypal_br_domain_routes` (924) |
| 代理检测 | `app.py:_test_proxy_candidate` (1977) |
| 错误分类诊断 | `app.py:_classify_attempt_error` (963) |
| 前置代理说明 | `README.md`「前置代理 FRONT_PROXY」 |

---

> 一句话总结:**提链 = 用 Token 下单 → Stripe 初始化 → 建 PayPal 支付方式 → confirm → approve 放行 → 跟 302 抓 PayPal 授权链。成功率的命门在"出口代理 + 出口国家 + 账号是否让 Stripe 开放 PayPal",其次是 Cookie 和尝试量。这几天一条没有,先从「换出口代理/换国家/换账号」开始,把日志分类读出来对症下药,而不是无脑重试同一个组合。**
